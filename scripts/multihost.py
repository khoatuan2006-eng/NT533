"""Render a three-VM lab, package an allowlisted release, and deploy via SSH/SCP."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import shlex
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEMENT = {
    "node1": [("service-a", "a-1", 50101), ("service-b", "b-1", 50201)],
    "node2": [("service-a", "a-2", 50102), ("service-c", "c-1", 50301), ("service-b", "b-3", 50203)],
    "node3": [("service-b", "b-2", 50202), ("service-c", "c-2", 50302)],
}


class ClusterDeployment:
    def __init__(self, addresses: list[str], ingress_port: int = 80):
        if not 1 <= ingress_port <= 65535:
            raise ValueError("Ingress port must be between 1 and 65535")
        self.ingress_port = ingress_port
        if len(addresses) != 3 or len(set(addresses)) != 3:
            raise ValueError("Exactly three distinct VM IPv4 addresses required")
        for address in addresses:
            ip = ipaddress.IPv4Address(address)
            if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
                raise ValueError("Use routable private VM addresses")
        self.nodes = dict(zip(PLACEMENT, addresses, strict=True))

    def render(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        topology = json.loads((ROOT / "configs/topology.compose.json").read_text())
        for service in topology["services"].values():
            service["instances"] = {}
        for node, instances in PLACEMENT.items():
            for service, instance, port in instances:
                topology["services"][service]["instances"][instance] = f"{self.nodes[node]}:{port}"
        self._write(directory / "topology.json", topology)
        bootstrap = {
            "xds_servers": [
                {
                    "server_uri": "ipv4:" + ",".join(f"{self.nodes[n]}:18000" for n in ("node1", "node2")),
                    "channel_creds": [{"type": "insecure"}],
                    "server_features": ["xds_v3"],
                }
            ],
            "node": {"id": "loadbalance-multihost"},
        }
        self._write(directory / "bootstrap.json", bootstrap)
        self._write(
            directory / "discovery.json",
            {
                "schema_version": 1,
                "services": {
                    "service-b": [],
                    "service-c": [],
                },
            },
        )
        for node, address in self.nodes.items():
            target = directory / node
            target.mkdir(exist_ok=True)
            services = {
                "consul": {
                    "image": "hashicorp/consul:2.1.0",
                    "network_mode": "host",
                    "restart": "unless-stopped",
                    "command": [
                        "agent",
                        "-server",
                        "-bootstrap-expect=3",
                        f"-node=lb-{node}",
                        f"-bind={address}",
                        f"-advertise={address}",
                        "-client=127.0.0.1",
                        "-datacenter=lb-cloud",
                        *[f"-retry-join={peer}" for peer in self.nodes.values()],
                    ],
                    "volumes": [
                        "consul-data:/consul/data",
                        "../../configs/consul-ha.json:/consul/config/ha.json:ro",
                    ],
                }
            }
            common = {
                "image": "loadbalance-workload:cloud",
                "build": {"context": "../..", "dockerfile": "deploy/Dockerfile"},
                "network_mode": "host",
                "restart": "unless-stopped",
                "command": ["python", "-m", "grpc_lb.microservices"],
                "volumes": [
                    "../topology.json:/app/configs/topology.json:ro",
                    "../bootstrap.json:/app/configs/bootstrap.json:ro",
                ],
            }
            for service, instance, port in PLACEMENT[node]:
                services[instance] = {
                    **common,
                    "environment": {
                        "SERVICE_ID": service,
                        "INSTANCE_ID": instance,
                        "LISTEN_ADDRESS": f"{address}:{port}",
                        "SERVICE_ADVERTISE_ADDRESS": f"{address}:{port}",
                        "TOPOLOGY_PATH": "/app/configs/topology.json",
                        "DISCOVERY_MODE": "xds",
                        "GRPC_XDS_BOOTSTRAP": "/app/configs/bootstrap.json",
                        "CONSUL_HTTP_ADDR": "http://127.0.0.1:8500",
                        "DRAIN_FILE": "/tmp/drain",
                    },
                }
            if node in ("node1", "node2"):
                services["controlplane"] = {
                    "image": "loadbalance-controlplane:cloud",
                    "build": {"context": "../..", "dockerfile": "deploy/Dockerfile.controlplane"},
                    "network_mode": "host",
                    "restart": "unless-stopped",
                    "volumes": ["../discovery.json:/config/discovery.json:ro"],
                    "environment": {
                        "CONSUL_HTTP_ADDR": "http://127.0.0.1:8500",
                        "CONSUL_ACTIVE_HEALTH": "1",
                        "XDS_LISTEN_ADDRESS": f"{address}:18000",
                    },
                }
                envoy = (ROOT / "deploy/envoy.microservices.yaml").read_text()
                envoy = envoy.replace(
                    "address: a-1, port_value: 50051", f"address: {self.nodes['node1']}, port_value: 50101"
                )
                envoy = envoy.replace(
                    "address: a-2, port_value: 50051", f"address: {self.nodes['node2']}, port_value: 50102"
                )
                (target / "envoy.yaml").write_text(envoy, encoding="utf-8")
                services["ingress"] = {
                    "image": "envoyproxy/envoy:v1.35.5",
                    "ports": [f"{self.ingress_port}:50050", "127.0.0.1:9901:9901"],
                    "restart": "unless-stopped",
                    "command": ["-c", "/etc/envoy/envoy.yaml", "--concurrency", "2"],
                    "volumes": ["./envoy.yaml:/etc/envoy/envoy.yaml:ro"],
                }
            self._write(
                target / "compose.json",
                {"services": services, "volumes": {"consul-data": {}}},
            )

    @staticmethod
    def _write(path: Path, value) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def bundle(output: Path, cluster: Path) -> str:
    """Only source/configuration enters the archive; never local keys or results."""
    output.parent.mkdir(parents=True, exist_ok=True)
    allowed = {".py", ".proto", ".json", ".yaml", ".toml", ".md"}
    candidates = [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / ".dockerignore"]
    for folder in ("src", "api", "configs", "deploy", "scripts", "tests", "docs"):
        candidates.extend((ROOT / folder).rglob("*"))
    with tarfile.open(output, "w:gz") as archive:
        for path in sorted(set(candidates)):
            if path.is_symlink() or not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix in allowed or path.name == ".dockerignore" or path.name.startswith("Dockerfile"):
                archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        for path in sorted(cluster.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.suffix in {".json", ".yaml"}:
                archive.add(path, arcname="cluster/" + path.relative_to(cluster).as_posix(), recursive=False)
    return hashlib.sha256(output.read_bytes()).hexdigest()


class SshDeployment:
    def __init__(self, key: Path, known_hosts: Path, config: Path | None = None):
        self.options = [
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
        ]
        if config is not None:
            if not config.is_file():
                raise ValueError("SSH config file does not exist")
            self.options = ["-F", str(config), *self.options]

    def run(self, host: str, script: str) -> str:
        ipaddress.IPv4Address(host)
        result = subprocess.run(
            ["ssh", *self.options, f"ubuntu@{host}", "bash -se"],
            input=script.replace("\r\n", "\n").encode("utf-8"),
            capture_output=True,
            timeout=1800,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"SSH {host} failed: {stdout[-2000:]} {stderr[-2000:]}")
        return stdout

    def upload(self, host: str, archive: Path, digest: str, node: str, *, prebuilt: bool = False) -> None:
        if node not in PLACEMENT:
            raise ValueError("Unknown node")
        ipaddress.IPv4Address(host)
        subprocess.run(
            ["scp", *self.options, str(archive), f"ubuntu@{host}:/tmp/loadbalance-release.tar.gz"],
            check=True,
            timeout=300,
        )
        if not self.run(host, "sha256sum /tmp/loadbalance-release.tar.gz\n").startswith(digest + " "):
            raise RuntimeError("Uploaded release checksum mismatch")
        script = """sudo mkdir -p /opt/loadbalance
sudo install -m 0644 /tmp/loadbalance-release.tar.gz /opt/loadbalance/release.tar.gz
sudo tar -xzf /tmp/loadbalance-release.tar.gz -C /opt/loadbalance
if ! command -v docker >/dev/null || ! sudo docker compose version >/dev/null 2>&1 || ! sudo docker buildx version >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 docker-buildx
fi
sudo systemctl enable --now docker
"""
        print(self.run(host, script), flush=True)
        compose = (
            f"sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/{shlex.quote(node)}/compose.json"
        )
        startup = "up --no-build --pull never -d" if prebuilt else "up --build -d"
        print(self.run(host, compose + " config --quiet\n" + compose + " " + startup + "\n"), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    render.add_argument("--nodes", nargs=3, required=True)
    render.add_argument("--ingress-port", type=int, default=80, help="Use a free port when sharing k3s hosts")
    render.add_argument("--output", type=Path, default=ROOT / "results/cloud-cluster")
    pack = commands.add_parser("bundle")
    pack.add_argument("--cluster", type=Path, required=True)
    pack.add_argument("--output", type=Path, default=ROOT / "results/loadbalance-release.tar.gz")
    deploy = commands.add_parser("deploy")
    deploy.add_argument(
        "--inventory", type=Path, required=True, help="JSON mapping node1/2/3 to SSH IPv4 addresses"
    )
    deploy.add_argument("--key", type=Path, required=True)
    deploy.add_argument("--ssh-config", type=Path, help="Optional SSH configuration, including ProxyJump")
    deploy.add_argument("--archive", type=Path, required=True)
    deploy.add_argument(
        "--prebuilt", action="store_true", help="Use verified preloaded images; never build/pull"
    )
    args = parser.parse_args()
    if args.command == "render":
        ClusterDeployment(args.nodes, args.ingress_port).render(args.output)
    elif args.command == "bundle":
        print(bundle(args.output, args.cluster))
    else:
        inventory = json.loads(args.inventory.read_text())
        if set(inventory) != set(PLACEMENT):
            raise ValueError("Inventory must contain node1, node2 and node3")
        for host in inventory.values():
            ipaddress.IPv4Address(host)
        ssh = SshDeployment(args.key, ROOT / "results/cloud-known-hosts", args.ssh_config)
        # Deploying restarts every container: never do it under a teammate's running experiment.
        holder = ssh.run(
            inventory["node1"], "cat /var/tmp/loadbalance-lab.lock/owner 2>/dev/null || true"
        ).strip()
        if holder:
            raise SystemExit(f"Cụm đang được dùng bởi {holder}; chờ xong rồi mới triển khai.")
        digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
        for node, host in inventory.items():
            ssh.upload(host, args.archive, digest, node, prebuilt=args.prebuilt)


if __name__ == "__main__":
    main()
