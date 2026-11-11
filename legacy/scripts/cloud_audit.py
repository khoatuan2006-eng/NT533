"""Save final VM, container, registry, ingress and Kubernetes status without secrets."""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloud_api import Cloud

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument(
        "--revoke-credential", action="store_true", help="Only after all API work is finished"
    )
    args = parser.parse_args()
    inventory = json.loads((ROOT / "results/k3s-ssh.json").read_text())
    config = ROOT / "results/k3s-ssh-config"

    def ssh(host, script):
        for attempt in range(3):
            result = subprocess.run(
                ["ssh", "-F", str(config), host, "bash -se"],
                input=script.encode(),
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0:
                return result.stdout.decode()
            if result.returncode != 255 or attempt == 2:
                raise RuntimeError(result.stderr.decode(errors="replace"))
            time.sleep(1)

    def node_status(item):
        node, host = item
        output = ssh(
            host, 'hostname\ncat /proc/sys/kernel/random/boot_id\nsudo docker ps --format "{{json .}}"\n'
        )
        hostname, boot_id, *containers = output.splitlines()
        image_ids = ssh(
            host,
            "sudo docker image inspect --format '{{.Id}}' loadbalance-workload:cloud loadbalance-controlplane:cloud\n",
        ).splitlines()
        return node, {
            "hostname": hostname,
            "boot_id": boot_id,
            "ssh": host,
            "containers": [json.loads(line) for line in containers],
            "image_ids": image_ids,
        }

    report = {
        "started_unix_s": time.time(),
        "release_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        report["nodes"] = dict(pool.map(node_status, inventory.items()))
    report["kubernetes_nodes"] = ssh(inventory["node1"], "sudo k3s kubectl get nodes\n")
    report["consul_raft"] = json.loads(
        ssh(inventory["node1"], "curl --fail --silent http://127.0.0.1:8500/v1/operator/raft/configuration\n")
    )
    cloud = Cloud(args.credentials)
    report["ingress"] = cloud.request(
        "load-balancer", "GET", "/v2/lbaas/loadbalancers/62877bb9-c24d-4f24-8f57-cb6f4491ece7/status"
    )
    report["vms"] = []
    for identity in (
        "50ffce03-5bfc-4137-90aa-a9960f8a09c7",
        "802e661e-3b47-4a20-b015-80ee7e38ddae",
        "2a2e7313-fa2b-4013-9f0e-c2fc5fd3496c",
    ):
        server = cloud.request("compute", "GET", f"/servers/{identity}")["server"]
        report["vms"].append({key: server.get(key) for key in ("id", "name", "status")})
    graph = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_microservices.py"),
            "--address",
            "192.168.120.109:80",
            "--topology",
            str(ROOT / "results/k3s-cluster/topology.json"),
            "--requests",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    graph.check_returncode()
    report["graph"] = json.loads(graph.stdout)
    containers = [c for node in report["nodes"].values() for c in node["containers"]]
    lb = report["ingress"]["statuses"]["loadbalancer"]
    report["checks"] = {
        "thirteen_running_containers": len(containers) == 13
        and all(c["State"] == "running" for c in containers),
        "three_active_vms": all(v["status"] == "ACTIVE" for v in report["vms"]),
        "three_raft_voters": len(report["consul_raft"]["Servers"]) == 3
        and all(v["Voter"] for v in report["consul_raft"]["Servers"]),
        "ingress_online": lb["operating_status"] == "ONLINE" and lb["provisioning_status"] == "ACTIVE",
        "same_workload_and_controlplane_images": len(
            {tuple(n["image_ids"]) for n in report["nodes"].values()}
        )
        == 1,
        "kubernetes_nodes_ready": report["kubernetes_nodes"].count(" Ready ") == 3,
    }
    report["finished_unix_s"] = time.time()
    report["credential_revoked"] = False
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not all(report["checks"].values()):
        raise RuntimeError(f"Final audit checks failed: {report['checks']}")
    if args.revoke_credential:
        cloud.revoke_own_credential()
        report["credential_revoked"] = True
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Audit saved; credential revoked:", report["credential_revoked"])


if __name__ == "__main__":
    main()
