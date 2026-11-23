"""Render a multi-VM lab from a cluster spec, package an allowlisted release, deploy and check it.

Three-node lab (the fault experiments):
    multihost.py render --nodes IP1 IP2 IP3 --output results/cloud-cluster
Any number of nodes (capacity runs), described in a spec file, see configs/cluster.example.json:
    multihost.py render --spec configs/cluster.scale.json --output results/scale-cluster
    multihost.py bundle --cluster results/scale-cluster --output results/release.tar.gz
    multihost.py deploy --spec configs/cluster.scale.json --key KEY --archive results/release.tar.gz
    multihost.py verify --spec configs/cluster.scale.json --key KEY      # after deploy, before measuring
    multihost.py stats  --spec configs/cluster.scale.json --key KEY --seconds 120 --output DIR
    multihost.py plan   --target-rps 16700 --capacity a=900 b=1400 c=1400 --vcpus 4
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import ipaddress
import json
import math
import re
import shlex
import statistics
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {"a": "service-a", "b": "service-b", "c": "service-c"}
# Instance n of a service listens on PORT_BASE + n (a-1 -> 50101, b-3 -> 50203).
PORT_BASE = {"service-a": 50100, "service-b": 50200, "service-c": 50300}
MAX_INSTANCES_PER_SERVICE = 99
RESERVED_PORTS = f"{min(PORT_BASE.values()) + 1}-{max(PORT_BASE.values()) + MAX_INSTANCES_PER_SERVICE}"
INSTANCE = re.compile(r"(?P<kind>[abc])-(?P<number>[1-9][0-9]?)")
# The three-node fault lab. Two copies of one component never share a node.
PLACEMENT = {
    "node1": [("service-a", "a-1", 50101), ("service-b", "b-1", 50201)],
    "node2": [("service-a", "a-2", 50102), ("service-c", "c-1", 50301), ("service-b", "b-3", 50203)],
    "node3": [("service-b", "b-2", 50202), ("service-c", "c-2", 50302)],
}
CONSUL_IMAGE = "hashicorp/consul:2.1.0"
ENVOY_IMAGE = "envoyproxy/envoy:v1.35.5"
# Docker keeps container logs forever by default; a long capacity run must not fill the disk.
LOGGING = {"driver": "json-file", "options": {"max-size": "50m", "max-file": "3"}}
CONSUL_PORTS = {"rpc": 8300, "serf": 8301, "http": 8500}
XDS_PORT = 18000
SERVICE_A_METRICS_PORT = 9200  # each A instance; one A per node
CONTROLPLANE_METRICS_PORT = 9201
REMOTE_DIR = "/opt/loadbalance/cluster"
PROJECT = "lb-cloud"


def _private_ip(value: str, where: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value)
    except ValueError as error:
        raise ValueError(f"{where}: '{value}' không phải địa chỉ IPv4 (điền IP private của VM)") from error
    if not ip.is_private or ip.is_loopback or ip.is_unspecified or ip.is_multicast:
        raise ValueError(f"{where}: use a routable private VM address, not {value}")
    return value


def instance_service(name: str) -> tuple[str, int]:
    match = INSTANCE.fullmatch(name)
    if not match:
        raise ValueError(f"instance name must look like a-1, b-12 or c-3: {name}")
    return SERVICES[match["kind"]], int(match["number"])


@dataclass
class NodeSpec:
    name: str
    ip: str
    ssh: str = ""
    vcpus: int = 2
    instances: list[str] = field(default_factory=list)


@dataclass
class ClusterSpec:
    """Where every component runs. Validated completely before anything is written."""

    nodes: dict[str, NodeSpec]
    consul_servers: list[str]
    controlplanes: list[str]
    ingress: list[str]
    ingress_port: int = 80
    envoy_concurrency: int = 2
    concurrency: dict[str, int] = field(default_factory=dict)
    request_log: str = "all"
    envoy_access_log: str = "all"
    address: str = ""
    ssh_config: str = ""

    @classmethod
    def three_nodes(cls, addresses: list[str], ingress_port: int = 80) -> ClusterSpec:
        if len(addresses) != 3 or len(set(addresses)) != 3:
            raise ValueError("Exactly three distinct VM IPv4 addresses required")
        nodes = {
            name: NodeSpec(name, ip, instances=[instance for _, instance, _ in PLACEMENT[name]])
            for name, ip in zip(PLACEMENT, addresses, strict=True)
        }
        spec = cls(nodes, ["node1", "node2", "node3"], ["node1", "node2"], ["node1", "node2"], ingress_port)
        spec.validate()
        return spec

    @classmethod
    def from_file(cls, path: Path) -> ClusterSpec:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {
            "schema_version", "nodes", "consul_servers", "controlplanes", "ingress", "ingress_port",
            "envoy_concurrency", "concurrency", "request_log", "envoy_access_log", "address", "ssh_config",
            "comment",
        }  # fmt: skip
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or set(raw) - known:
            raise ValueError(f"spec needs schema_version 1 and only these keys: {', '.join(sorted(known))}")
        if not isinstance(raw.get("nodes"), dict) or not raw["nodes"]:
            raise ValueError("spec.nodes must be a nonempty object")
        nodes, counters = {}, {service: 0 for service in SERVICES.values()}
        for name, node in raw["nodes"].items():
            if not isinstance(node, dict) or set(node) - {"ip", "ssh", "vcpus", "instances", "comment"}:
                raise ValueError(f"nodes.{name}: only ip, ssh, vcpus and instances are allowed")
            instances = node.get("instances", [])
            if isinstance(instances, dict):
                # {"service-a": 2} -> the next free numbers of that service, in node order.
                expanded = []
                for service, count in instances.items():
                    if service not in counters or type(count) is not int or count < 0:
                        raise ValueError(f"nodes.{name}.instances: count per service-a/b/c expected")
                    prefix = service.removeprefix("service-")
                    for _ in range(count):
                        counters[service] += 1
                        expanded.append(f"{prefix}-{counters[service]}")
                instances = expanded
            if not isinstance(instances, list):
                raise ValueError(f"nodes.{name}.instances must be a list of names or counts per service")
            nodes[name] = NodeSpec(
                name, str(node.get("ip", "")), str(node.get("ssh", "")), node.get("vcpus", 2), list(instances)
            )
        spec = cls(
            nodes,
            list(raw.get("consul_servers", [])),
            list(raw.get("controlplanes", [])),
            list(raw.get("ingress", [])),
            raw.get("ingress_port", 80),
            raw.get("envoy_concurrency", 2),
            dict(raw.get("concurrency", {})),
            raw.get("request_log", "all"),
            raw.get("envoy_access_log", "all"),
            raw.get("address", ""),
            raw.get("ssh_config", ""),
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        if type(self.ingress_port) is not int or not 1 <= self.ingress_port <= 65535:
            raise ValueError("Ingress port must be between 1 and 65535")
        if type(self.envoy_concurrency) is not int or not 1 <= self.envoy_concurrency <= 64:
            raise ValueError("envoy_concurrency must be 1..64")
        if self.request_log not in ("all", "errors", "off") or self.envoy_access_log not in ("all", "errors"):
            raise ValueError("request_log: all/errors/off; envoy_access_log: all/errors")
        ips, seen = set(), set()
        counts = {service: 0 for service in SERVICES.values()}
        for name, node in self.nodes.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name):
                raise ValueError(f"node name must be lowercase letters, digits and '-': {name}")
            _private_ip(node.ip, f"nodes.{name}.ip")
            if node.ip in ips:
                raise ValueError(f"two nodes share {node.ip}")
            ips.add(node.ip)
            if type(node.vcpus) is not int or not 1 <= node.vcpus <= 256:
                raise ValueError(f"nodes.{name}.vcpus must be 1..256")
            for instance in node.instances:
                service, number = instance_service(instance)
                if number > MAX_INSTANCES_PER_SERVICE:
                    raise ValueError(f"at most {MAX_INSTANCES_PER_SERVICE} instances per service")
                if instance in seen:
                    raise ValueError(f"instance placed twice: {instance}")
                seen.add(instance)
                counts[service] += 1
        for service, count in counts.items():
            # C is optional: without it, A calls B only (the single-dependency layout).
            if not count and service != "service-c":
                raise ValueError(f"spec places no instance of {service}")
        for role, members in (
            ("consul_servers", self.consul_servers),
            ("controlplanes", self.controlplanes),
            ("ingress", self.ingress),
        ):
            if not members or len(set(members)) != len(members) or set(members) - set(self.nodes):
                raise ValueError(f"{role} must list distinct, known nodes")
        if len(self.consul_servers) % 2 == 0 or len(self.consul_servers) > 7:
            # Even sizes add a voter without adding fault tolerance; Raft writes slow down past 7.
            raise ValueError("consul_servers must have 1, 3, 5 or 7 members")
        for service, value in self.concurrency.items():
            if service not in counts or type(value) is not int or not 1 <= value <= 10_000:
                raise ValueError("concurrency maps service-a/b/c to 1..10000")

    def placement(self) -> dict[str, list[tuple[str, str, int]]]:
        result = {}
        for name, node in self.nodes.items():
            result[name] = []
            for instance in node.instances:
                service, number = instance_service(instance)
                result[name].append((service, instance, PORT_BASE[service] + number))
        return result

    def endpoints(self, service: str) -> list[tuple[str, str, int]]:
        """(instance, ip, port) of every instance of one service, in instance-number order."""
        rows = [
            (instance, self.nodes[node].ip, port)
            for node, items in self.placement().items()
            for name, instance, port in items
            if name == service
        ]
        return sorted(rows, key=lambda row: instance_service(row[0])[1])

    def lab_names(self) -> dict[str, dict[str, str]]:
        """Lab service name -> compose service name, per node (the labenv description)."""
        names = {node: {} for node in self.nodes}
        for index, node in enumerate(self.consul_servers, 1):
            names[node][f"consul-{index}"] = "consul"
        for node in self.nodes:
            if node not in self.consul_servers:
                names[node][f"consul-agent-{node}"] = "consul"
        for index, node in enumerate(self.controlplanes, 1):
            names[node][f"controlplane-{index}"] = "controlplane"
        for index, node in enumerate(self.ingress, 1):
            names[node][f"envoy-{index}"] = "ingress"
        for node, items in self.placement().items():
            for _, instance, _ in items:
                names[node][instance] = instance
        return names

    def to_json(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                name: {"ip": n.ip, "ssh": n.ssh, "vcpus": n.vcpus, "instances": n.instances}
                for name, n in self.nodes.items()
            },
            "consul_servers": self.consul_servers,
            "controlplanes": self.controlplanes,
            "ingress": self.ingress,
            "ingress_port": self.ingress_port,
            "envoy_concurrency": self.envoy_concurrency,
            "concurrency": self.concurrency,
            "request_log": self.request_log,
            "envoy_access_log": self.envoy_access_log,
            "address": self.address,
            "ssh_config": self.ssh_config,
        }


def envoy_breakers(a_instances: int, a_concurrency: int, envoy_concurrency: int) -> dict[str, int]:
    """Ingress limits that scale with the A tier instead of a fixed 100 requests.

    Python services collapse when overloaded (requests queue past their deadline), so Envoy
    must refuse extra work cheaply instead of forwarding it: in-flight requests per Envoy are
    capped at what all A instances admit. Three-node lab: 2 x 50 -> 100/32/16 as before.
    """
    total = a_instances * a_concurrency
    return {
        "max_requests": total,
        "max_pending_requests": max(32, total // 4),
        "max_connections": max(16, a_instances * envoy_concurrency * 2),
    }


ENVOY_ENDPOINTS = """              - endpoint:
                  address:
                    socket_address: {address: a-1, port_value: 50051}
              - endpoint:
                  address:
                    socket_address: {address: a-2, port_value: 50051}
"""


def render_envoy(template: str, endpoints: list[tuple[str, str, int]], limits: dict, access_log: str) -> str:
    if ENVOY_ENDPOINTS not in template:
        raise ValueError("deploy/envoy.microservices.yaml changed; update ENVOY_ENDPOINTS in multihost.py")
    block = "".join(
        "              - endpoint:\n"
        "                  address:\n"
        f"                    socket_address: {{address: {ip}, port_value: {port}}}\n"
        for _, ip, port in endpoints
    )
    text = template.replace(ENVOY_ENDPOINTS, block)
    for key, value in limits.items():
        text, count = re.subn(rf"(\n\s+{key}: )\d+", rf"\g<1>{value}", text, count=1)
        if count != 1:
            raise ValueError(f"envoy template has no {key}")
    if access_log == "errors":
        anchor = "                  - name: envoy.access_loggers.stdout\n"
        if anchor not in text:
            raise ValueError("envoy template has no stdout access log")
        text = text.replace(
            anchor,
            anchor + "                    filter:\n"
            "                      grpc_status_filter: {statuses: [OK], exclude: true}\n",
        )
    return text


class ClusterDeployment:
    def __init__(self, nodes: list[str] | ClusterSpec, ingress_port: int = 80):
        self.spec = nodes if isinstance(nodes, ClusterSpec) else ClusterSpec.three_nodes(nodes, ingress_port)
        self.spec.validate()
        self.ingress_port = self.spec.ingress_port
        self.nodes = {name: node.ip for name, node in self.spec.nodes.items()}

    def render(self, directory: Path) -> None:
        spec = self.spec
        directory.mkdir(parents=True, exist_ok=True)
        topology = json.loads((ROOT / "configs/topology.compose.json").read_text())
        for name, service in topology["services"].items():
            service["instances"] = {}
            if name in spec.concurrency:
                service["concurrency"] = spec.concurrency[name]
        for node, instances in spec.placement().items():
            for service, instance, port in instances:
                topology["services"][service]["instances"][instance] = f"{self.nodes[node]}:{port}"
        for service in topology["services"].values():
            service["instances"] = dict(
                sorted(service["instances"].items(), key=lambda i: instance_service(i[0])[1])
            )
        # A service the spec places nowhere leaves the call graph instead of failing every request.
        absent = [name for name, service in topology["services"].items() if not service["instances"]]
        for name in absent:
            del topology["services"][name]
        for service in topology["services"].values():
            if "dependencies" in service:
                service["dependencies"] = [d for d in service["dependencies"] if d not in absent]
        self._write(directory / "topology.json", topology)
        self._write(
            directory / "bootstrap.json",
            {
                "xds_servers": [
                    {
                        "server_uri": "ipv4:"
                        + ",".join(f"{self.nodes[n]}:{XDS_PORT}" for n in spec.controlplanes),
                        "channel_creds": [{"type": "insecure"}],
                        "server_features": ["xds_v3"],
                    }
                ],
                "node": {"id": "loadbalance-multihost"},
            },
        )
        self._write(
            directory / "discovery.json",
            {
                "schema_version": 1,
                "services": {name: [] for name in ("service-b", "service-c") if name in topology["services"]},
            },
        )
        a_endpoints = spec.endpoints("service-a")
        a_concurrency = topology["services"]["service-a"].get("concurrency", 50)
        limits = envoy_breakers(len(a_endpoints), a_concurrency, spec.envoy_concurrency)
        template = (ROOT / "deploy/envoy.microservices.yaml").read_text()
        servers = [self.nodes[n] for n in spec.consul_servers]
        for node, address in self.nodes.items():
            target = directory / node
            target.mkdir(exist_ok=True)
            agent = [
                "agent",
                *(["-server", f"-bootstrap-expect={len(servers)}"] if node in spec.consul_servers else []),
                f"-node=lb-{node}",
                f"-bind={address}",
                f"-advertise={address}",
                # Servers also answer on the private address: control planes on other nodes read
                # through them when their own agent is gone (the consul-leader r1 failure).
                f"-client=127.0.0.1 {address}" if node in spec.consul_servers else "-client=127.0.0.1",
                "-datacenter=lb-cloud",
                *[f"-retry-join={peer}" for peer in servers],
            ]
            services = {
                "consul": {
                    "image": CONSUL_IMAGE,
                    "network_mode": "host",
                    "restart": "unless-stopped",
                    "command": agent,
                    "volumes": [
                        "consul-data:/consul/data",
                        "../../configs/consul-ha.json:/consul/config/ha.json:ro",
                    ],
                    "logging": LOGGING,
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
                "logging": LOGGING,
            }
            for service, instance, port in spec.placement()[node]:
                environment = {
                    "SERVICE_ID": service,
                    "INSTANCE_ID": instance,
                    "LISTEN_ADDRESS": f"{address}:{port}",
                    "SERVICE_ADVERTISE_ADDRESS": f"{address}:{port}",
                    "TOPOLOGY_PATH": "/app/configs/topology.json",
                    "DISCOVERY_MODE": "xds",
                    "GRPC_XDS_BOOTSTRAP": "/app/configs/bootstrap.json",
                    "CONSUL_HTTP_ADDR": "http://127.0.0.1:8500",
                    "DRAIN_FILE": "/tmp/drain",
                }
                if spec.request_log != "all":
                    environment["REQUEST_LOG"] = spec.request_log
                if service == "service-a":
                    environment["METRICS_PORT"] = str(SERVICE_A_METRICS_PORT)
                services[instance] = {**common, "environment": environment}
            if node in spec.controlplanes:
                # Local agent first, then every server: losing one agent is not losing quorum.
                consul = ["http://127.0.0.1:8500"] + [
                    f"http://{ip}:{CONSUL_PORTS['http']}" for ip in servers if ip != address
                ]
                services["controlplane"] = {
                    "image": "loadbalance-controlplane:cloud",
                    "build": {"context": "../..", "dockerfile": "deploy/Dockerfile.controlplane"},
                    "network_mode": "host",
                    "restart": "unless-stopped",
                    "volumes": ["../discovery.json:/config/discovery.json:ro"],
                    "environment": {
                        "CONSUL_HTTP_ADDR": ",".join(consul),
                        "CONSUL_ACTIVE_HEALTH": "1",
                        "XDS_LISTEN_ADDRESS": f"{address}:{XDS_PORT}",
                        # Weight controller metrics (beta, weights, fallback) for Prometheus.
                        "METRICS_PORT": str(CONTROLPLANE_METRICS_PORT),
                    },
                    "logging": LOGGING,
                }
            if node in spec.ingress:
                (target / "envoy.yaml").write_text(
                    render_envoy(template, a_endpoints, limits, spec.envoy_access_log), encoding="utf-8"
                )
                services["ingress"] = {
                    "image": ENVOY_IMAGE,
                    "ports": [f"{spec.ingress_port}:50050", "9901:9901"],
                    "restart": "unless-stopped",
                    "command": ["-c", "/etc/envoy/envoy.yaml", "--concurrency", str(spec.envoy_concurrency)],
                    "volumes": ["./envoy.yaml:/etc/envoy/envoy.yaml:ro"],
                    "logging": LOGGING,
                }
            self._write(target / "compose.json", {"services": services, "volumes": {"consul-data": {}}})
        self._write(directory / "cluster.json", spec.to_json())
        if all(node.ssh for node in spec.nodes.values()):
            ingress = spec.address or f"{self.nodes[spec.ingress[0]]}:{spec.ingress_port}"
            self._write(
                directory / "lab.json",
                {
                    "driver": "ssh",
                    "address": ingress,
                    "ssh_config": spec.ssh_config or "configs/ssh-config",
                    "project": PROJECT,
                    "remote_dir": REMOTE_DIR,
                    "nodes": {
                        name: {"ssh": spec.nodes[name].ssh, "ip": spec.nodes[name].ip, "services": services}
                        for name, services in spec.lab_names().items()
                    },
                },
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

    def run(self, host: str, script: str, *, check: bool = True, timeout: float = 1800) -> str:
        ipaddress.IPv4Address(host)
        result = subprocess.run(
            ["ssh", *self.options, f"ubuntu@{host}", "bash -se"],
            input=script.replace("\r\n", "\n").encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if check and result.returncode:
            raise RuntimeError(f"SSH {host} failed: {stdout[-2000:]} {stderr[-2000:]}")
        return stdout

    def upload(
        self,
        host: str,
        archive: Path,
        digest: str,
        node: str,
        *,
        prebuilt: bool = False,
        builds: list[str] | None = None,
    ) -> str:
        if builds is None:
            if node not in PLACEMENT:
                raise ValueError("Unknown node")
            builds = [PLACEMENT[node][0][1]] + (["controlplane"] if node in ("node1", "node2") else [])
        ipaddress.IPv4Address(host)
        subprocess.run(
            ["scp", *self.options, str(archive), f"ubuntu@{host}:/tmp/loadbalance-release.tar.gz"],
            check=True,
            timeout=300,
            capture_output=True,
        )
        if not self.run(host, "sha256sum /tmp/loadbalance-release.tar.gz\n").startswith(digest + " "):
            raise RuntimeError("Uploaded release checksum mismatch")
        script = f"""# Service ports sit inside Linux's ephemeral range: an outgoing connection may take one
# as its source port and a restarting instance then fails with 'Address already in use'.
echo 'net.ipv4.ip_local_reserved_ports = {RESERVED_PORTS}' | sudo tee /etc/sysctl.d/90-loadbalance.conf >/dev/null
sudo sysctl -q -w net.ipv4.ip_local_reserved_ports={RESERVED_PORTS}
sudo mkdir -p /opt/loadbalance
sudo install -m 0644 /tmp/loadbalance-release.tar.gz /opt/loadbalance/release.tar.gz
sudo tar -xzf /tmp/loadbalance-release.tar.gz -C /opt/loadbalance
if ! command -v docker >/dev/null || ! sudo docker compose version >/dev/null 2>&1 || ! sudo docker buildx version >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 docker-buildx
fi
sudo systemctl enable --now docker
"""
        output = self.run(host, script)
        compose = f"sudo docker compose -p {PROJECT} -f {REMOTE_DIR}/{shlex.quote(node)}/compose.json"
        commands = [f"{compose} config --quiet"]
        if not prebuilt and builds:
            # All workloads share an image tag. Build it once to avoid concurrent exports to that tag.
            commands.append(f"{compose} build {shlex.join(builds)}")
        pull = " --pull never" if prebuilt else ""
        commands.append(f"{compose} up --no-build{pull} -d --remove-orphans")
        return output + self.run(host, "\n".join(commands) + "\n")


def builds_for(spec: ClusterSpec, node: str) -> list[str]:
    """Compose services to build on a node: one workload (shared image) and the control plane."""
    targets = [spec.nodes[node].instances[0]] if spec.nodes[node].instances else []
    return targets + (["controlplane"] if node in spec.controlplanes else [])


def parallel(nodes: list[str], action, *, workers: int = 8) -> dict[str, object]:
    """Run action(node) on every node at once; collect results and errors per node."""
    results: dict[str, object] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(nodes)))) as pool:
        futures = {pool.submit(action, node): node for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                results[node] = future.result()
            except Exception as error:  # reported per node, never hides the others
                results[node] = error
    return {node: results[node] for node in nodes}


def connectivity_checks(spec: ClusterSpec) -> dict[str, list[tuple[str, str, int]]]:
    """(label, ip, port) each node must reach. Misconfigured security groups show up here."""
    ip = {name: node.ip for name, node in spec.nodes.items()}
    placement = spec.placement()
    checks: dict[str, list[tuple[str, str, int]]] = {name: [] for name in spec.nodes}
    for node in spec.nodes:
        for server in spec.consul_servers:
            if server != node:
                checks[node].append((f"consul-rpc {server}", ip[server], CONSUL_PORTS["rpc"]))
                checks[node].append((f"consul-serf {server}", ip[server], CONSUL_PORTS["serf"]))
        if node in spec.controlplanes:
            for server in spec.consul_servers:
                checks[node].append((f"consul-http {server}", ip[server], CONSUL_PORTS["http"]))
            for owner, items in placement.items():
                for _, instance, port in items:
                    if instance[0] in "bc":
                        checks[node].append((f"probe {instance}", ip[owner], port))
        if any(instance[0] == "a" for _, instance, _ in placement[node]):
            for plane in spec.controlplanes:
                checks[node].append((f"xds {plane}", ip[plane], XDS_PORT))
            for owner, items in placement.items():
                for _, instance, port in items:
                    if instance[0] in "bc":
                        checks[node].append((f"call {instance}", ip[owner], port))
            checks[node].append((f"metrics-a {node}", ip[node], SERVICE_A_METRICS_PORT))
        if node in spec.controlplanes:
            checks[node].append((f"metrics-controlplane {node}", ip[node], CONTROLPLANE_METRICS_PORT))
        if node in spec.ingress:
            for instance, address, port in spec.endpoints("service-a"):
                checks[node].append((f"envoy->{instance}", address, port))
    return checks


def verify_script(spec: ClusterSpec, node: str) -> str:
    compose = f"sudo docker compose -p {PROJECT} -f {REMOTE_DIR}/{node}/compose.json"
    lines = [
        "set +e",
        f"echo RUNNING $({compose} ps --status running --services 2>/dev/null | sort | tr '\\n' ' ')",
        "echo CPUS $(nproc)",
        "echo DISK $(df --output=pcent / | tail -1 | tr -d ' %')",
    ]
    for label, address, port in connectivity_checks(spec)[node]:
        lines.append(
            f"if timeout 1 bash -c '</dev/tcp/{address}/{port}' 2>/dev/null; then :; "
            f"else echo 'UNREACHABLE {label} {address}:{port}'; fi"
        )
    if node == spec.consul_servers[0]:
        lines += [
            f"echo MEMBERS $({compose} exec -T consul consul members -status=alive 2>/dev/null | tail -n +2 | wc -l)",
            f"echo VOTERS $({compose} exec -T consul consul operator raft list-peers 2>/dev/null "
            "| tail -n +2 | wc -l)",
        ]
    return "\n".join(lines) + "\n"


def expected_services(spec: ClusterSpec, node: str) -> set[str]:
    names = {"consul", *spec.nodes[node].instances}
    if node in spec.controlplanes:
        names.add("controlplane")
    if node in spec.ingress:
        names.add("ingress")
    return names


def verify(spec: ClusterSpec, ssh: SshDeployment) -> list[str]:
    """Every problem found, in plain words; empty means the cluster is ready to measure."""
    outputs = parallel(
        list(spec.nodes), lambda node: ssh.run(spec.nodes[node].ssh, verify_script(spec, node), timeout=300)
    )
    problems = []
    for node, output in outputs.items():
        if isinstance(output, Exception):
            problems.append(f"{node}: không SSH được ({output})")
            continue
        values = {}
        for line in output.splitlines():
            key, _, rest = line.partition(" ")
            if key == "UNREACHABLE":
                problems.append(f"{node}: không tới được {rest} (security group / service chưa chạy?)")
            else:
                values[key] = rest
        running = set(values.get("RUNNING", "").split())
        missing = expected_services(spec, node) - running
        if missing:
            problems.append(f"{node}: container chưa chạy: {', '.join(sorted(missing))}")
        cpus = int(values.get("CPUS", "0") or 0)
        if cpus and cpus != spec.nodes[node].vcpus:
            problems.append(f"{node}: spec ghi {spec.nodes[node].vcpus} vCPU nhưng máy có {cpus}")
        if int(values.get("DISK", "0") or 0) >= 85:
            problems.append(f"{node}: ổ đĩa đã dùng {values['DISK']}%")
        if "MEMBERS" in values and int(values["MEMBERS"] or 0) != len(spec.nodes):
            problems.append(f"Consul thấy {values['MEMBERS']}/{len(spec.nodes)} node còn sống")
        if "VOTERS" in values and int(values["VOTERS"] or 0) != len(spec.consul_servers):
            problems.append(f"Raft có {values['VOTERS']}/{len(spec.consul_servers)} voter")
    return problems


def stats(spec: ClusterSpec, ssh: SshDeployment, seconds: float, output: Path) -> str:
    """Sample `docker stats` on every node at once; the container pinned near 100% is the bottleneck."""
    samples = max(1, int(seconds // 3))
    script = (
        f"for i in $(seq {samples}); do sudo docker stats --no-stream --format "
        "'{{.Name}},{{.CPUPerc}},{{.MemPerc}}' | sed \"s/^/$(date +%s),/\"; done\n"
    )
    outputs = parallel(
        list(spec.nodes),
        lambda node: ssh.run(spec.nodes[node].ssh, script, timeout=seconds + 120),
        workers=64,
    )
    output.mkdir(parents=True, exist_ok=True)
    rows, problems = [], []
    for node, text in outputs.items():
        if isinstance(text, Exception):
            problems.append(f"{node}: {text}")
            continue
        for line in text.splitlines():
            parts = line.split(",")
            if len(parts) == 4:
                stamp, name, cpu, mem = parts
                try:
                    rows.append((int(stamp), node, name, float(cpu.rstrip("%")), float(mem.rstrip("%"))))
                except ValueError:
                    continue
    (output / "stats.csv").write_text(
        "unix_s,node,container,cpu_percent,mem_percent\n"
        + "".join(f"{s},{n},{c},{u},{m}\n" for s, n, c, u, m in rows),
        encoding="utf-8",
    )
    per: dict[tuple[str, str], list[float]] = {}
    for _, node, name, cpu, _ in rows:
        per.setdefault((node, name), []).append(cpu)
    lines = [
        "# CPU theo container",
        "",
        "100% = một vCPU. Service Python gần 100% là đã bão hòa (một tiến trình chỉ dùng hiệu quả một lõi); "
        f"Envoy bão hòa ở khoảng {spec.envoy_concurrency * 100}%.",
        "",
        "| Node | Container | Trung bình (%) | Cao nhất (%) | Bão hòa? |",
        "|---|---|---:|---:|---|",
    ]
    for (node, name), values in sorted(per.items(), key=lambda item: -max(item[1])):
        limit = spec.envoy_concurrency * 100 if "ingress" in name else 100
        flag = "**có**" if max(values) >= 0.9 * limit else ""
        lines.append(f"| {node} | {name} | {statistics.mean(values):.0f} | {max(values):.0f} | {flag} |")
    for problem in problems:
        lines.append(f"\nLỗi lấy số liệu: {problem}")
    text = "\n".join(lines) + "\n"
    (output / "stats.md").write_text(text, encoding="utf-8")
    return text


def plan(target_rps: float, capacity: dict[str, float], vcpus: int, *, headroom: float, envoy_rps_per_core: float,
         envoy_concurrency: int) -> dict:  # fmt: skip
    """Instances and nodes for a target, from per-instance capacities measured on the cluster."""
    instances = {
        service: math.ceil(target_rps / (capacity[service] * headroom)) for service in SERVICES.values()
    }
    ingress = max(2, math.ceil(target_rps / (envoy_rps_per_core * envoy_concurrency * headroom)))
    # One vCPU per node stays free for Consul, Docker and the kernel; Envoy takes its cores.
    # A Python instance uses about one core, so a node holds (vcpus - 1) instances.
    worker_slots = max(1, vcpus - 1)
    ingress_slots = max(0, worker_slots - envoy_concurrency)
    remaining = max(0, sum(instances.values()) - ingress * ingress_slots)
    nodes = max(3, ingress + math.ceil(remaining / worker_slots))
    return {
        "target_rps": target_rps,
        "target_rpm": target_rps * 60,
        "instances": instances,
        "ingress_nodes": ingress,
        "nodes": nodes,
        "vcpus_per_node": vcpus,
        "total_vcpus": nodes * vcpus,
    }


def plan_spec(result: dict, envoy_concurrency: int) -> dict:
    """A spec skeleton: instances spread round-robin, same-service copies on different nodes."""
    nodes = [f"node{i}" for i in range(1, result["nodes"] + 1)]
    slots = {node: result["vcpus_per_node"] - 1 for node in nodes}
    ingress = nodes[: result["ingress_nodes"]]
    for node in ingress:
        slots[node] = max(0, slots[node] - envoy_concurrency)
    placed = {node: [] for node in nodes}
    cursor = 0
    for service, count in result["instances"].items():
        prefix = service.removeprefix("service-")
        for number in range(1, count + 1):
            for _ in range(len(nodes)):
                node = nodes[cursor % len(nodes)]
                cursor += 1
                if len(placed[node]) < max(1, slots[node]) or all(
                    len(placed[n]) >= max(1, slots[n]) for n in nodes
                ):
                    placed[node].append(f"{prefix}-{number}")
                    break
    return {
        "schema_version": 1,
        "comment": "Điền ip (private) và ssh (public) cho từng node, rồi render --spec file này.",
        "nodes": {
            node: {"ip": "FILL-PRIVATE-IP", "ssh": "FILL-PUBLIC-IP", "vcpus": result["vcpus_per_node"], "instances": placed[node]}
            for node in nodes
        },
        "consul_servers": nodes[:3] if len(nodes) < 5 else nodes[:5],
        "controlplanes": nodes[1:3] if len(nodes) > 2 else nodes[:2],
        "ingress": ingress,
        "ingress_port": 80,
        "envoy_concurrency": envoy_concurrency,
        "request_log": "errors",
        "envoy_access_log": "errors",
    }  # fmt: skip


def _ssh_from(args) -> SshDeployment:
    return SshDeployment(args.key, ROOT / "results/cloud-known-hosts", args.ssh_config)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render")
    source = render.add_mutually_exclusive_group(required=True)
    source.add_argument("--nodes", nargs=3, help="The three-node fault lab: private IPs of node1..3")
    source.add_argument("--spec", type=Path, help="Cluster spec file (any number of nodes)")
    render.add_argument("--ingress-port", type=int, default=80, help="With --nodes: Envoy's published port")
    render.add_argument("--output", type=Path, default=ROOT / "results/cloud-cluster")
    pack = commands.add_parser("bundle")
    pack.add_argument("--cluster", type=Path, required=True)
    pack.add_argument("--output", type=Path, default=ROOT / "results/loadbalance-release.tar.gz")
    for name in ("deploy", "verify", "stats"):
        sub = commands.add_parser(name)
        where = sub.add_mutually_exclusive_group(required=True)
        where.add_argument("--spec", type=Path, help="Cluster spec with ssh addresses")
        where.add_argument("--inventory", type=Path, help="Three-node lab: JSON node1/2/3 -> SSH IPv4")
        sub.add_argument("--key", type=Path, required=True)
        sub.add_argument("--ssh-config", type=Path, help="Optional SSH configuration, including ProxyJump")
        if name == "deploy":
            sub.add_argument("--archive", type=Path, required=True)
            sub.add_argument(
                "--prebuilt", action="store_true", help="Use verified preloaded images; never build"
            )
        if name == "stats":
            sub.add_argument("--seconds", type=float, default=120)
            sub.add_argument("--output", type=Path, required=True)
    planner = commands.add_parser("plan")
    planner.add_argument("--target-rps", type=float, required=True, help="1 000 000 RPM = 16667 RPS")
    planner.add_argument(
        "--capacity", nargs=3, required=True, metavar="X=RPS",
        help="Measured RPS one instance sustains at ~90%% CPU: a=… b=… c=…",
    )  # fmt: skip
    planner.add_argument("--vcpus", type=int, default=4, help="vCPUs per node")
    planner.add_argument(
        "--headroom", type=float, default=0.7, help="Run instances at this fraction of capacity"
    )
    planner.add_argument(
        "--envoy-rps-per-core", type=float, default=4000, help="Measure it; default is a guess"
    )
    planner.add_argument("--envoy-concurrency", type=int, default=2)
    planner.add_argument("--output", type=Path, help="Write a spec skeleton here")
    args = parser.parse_args()

    if args.command == "render":
        spec = (
            ClusterSpec.from_file(args.spec)
            if args.spec
            else ClusterSpec.three_nodes(args.nodes, args.ingress_port)
        )
        ClusterDeployment(spec).render(args.output)
        print(f"Đã sinh {len(spec.nodes)} node vào {args.output}")
        return
    if args.command == "bundle":
        print(bundle(args.output, args.cluster))
        return
    if args.command == "plan":
        capacity = {}
        for item in args.capacity:
            kind, _, value = item.partition("=")
            if kind not in SERVICES or not value:
                parser.error("--capacity takes a=RPS b=RPS c=RPS")
            capacity[SERVICES[kind]] = float(value)
        if not 0 < args.headroom <= 1 or args.vcpus < 2 or min(capacity.values()) <= 0:
            parser.error("headroom in (0, 1], vcpus >= 2, positive capacities")
        result = plan(
            args.target_rps, capacity, args.vcpus, headroom=args.headroom,
            envoy_rps_per_core=args.envoy_rps_per_core, envoy_concurrency=args.envoy_concurrency,
        )  # fmt: skip
        print(json.dumps(result, indent=2))
        if args.output:
            args.output.write_text(
                json.dumps(plan_spec(result, args.envoy_concurrency), indent=2) + "\n", encoding="utf-8"
            )
            print(f"Đã ghi khung spec vào {args.output}; điền IP rồi render --spec.")
        return

    if args.spec:
        spec = ClusterSpec.from_file(args.spec)
        if any(not node.ssh for node in spec.nodes.values()):
            parser.error("every node in the spec needs an ssh address")
        builds = {node: builds_for(spec, node) for node in spec.nodes}
    else:
        inventory = json.loads(args.inventory.read_text())
        if set(inventory) != set(PLACEMENT):
            raise ValueError("Inventory must contain node1, node2 and node3")
        for host in inventory.values():
            ipaddress.IPv4Address(host)
        # The inventory carries SSH addresses only; private IPs do not matter for these commands.
        spec = ClusterSpec.three_nodes(["10.255.0.1", "10.255.0.2", "10.255.0.3"])
        for node, host in inventory.items():
            spec.nodes[node].ssh = host
        builds = {node: None for node in spec.nodes}
    ssh = _ssh_from(args)
    first = spec.nodes[next(iter(spec.nodes))].ssh

    if args.command == "verify":
        if args.inventory:
            parser.error("verify needs --spec: it checks the private addresses between nodes")
        problems = verify(spec, ssh)
        if problems:
            print("CHƯA SẴN SÀNG:\n- " + "\n- ".join(problems))
            raise SystemExit(1)
        print(f"Sẵn sàng: {len(spec.nodes)} node, mọi container chạy, mọi cổng cần thiết đều tới được.")
        return
    if args.command == "stats":
        print(stats(spec, ssh, args.seconds, args.output))
        return

    # deploy. Deploying restarts every container: never do it under a teammate's running experiment.
    holder = ssh.run(first, "cat /var/tmp/loadbalance-lab.lock/owner 2>/dev/null || true").strip()
    if holder:
        raise SystemExit(f"Cụm đang được dùng bởi {holder}; chờ xong rồi mới triển khai.")
    digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    started = time.monotonic()
    results = parallel(
        list(spec.nodes),
        lambda node: ssh.upload(
            spec.nodes[node].ssh, args.archive, digest, node, prebuilt=args.prebuilt, builds=builds[node]
        ),
    )
    failed = []
    for node, result in results.items():
        if isinstance(result, Exception):
            failed.append(node)
            print(f"== {node}: LỖI\n{result}", flush=True)
        else:
            print(f"== {node}: xong\n{str(result).strip()[-1500:]}", flush=True)
    print(
        f"Triển khai {len(spec.nodes) - len(failed)}/{len(spec.nodes)} node trong {time.monotonic() - started:.0f} s."
    )
    if failed:
        raise SystemExit(f"Node lỗi: {', '.join(failed)}. Sửa rồi chạy lại deploy (an toàn khi lặp lại).")


if __name__ == "__main__":
    main()
