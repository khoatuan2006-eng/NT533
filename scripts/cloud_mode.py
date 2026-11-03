"""Blocking mode hook for controlled_suite, executed on the SSH operator machine."""

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["static", "xds"])
    parser.add_argument("--ssh-config", type=Path, default=ROOT / "results/k3s-ssh-config")
    parser.add_argument("--inventory", type=Path, default=ROOT / "results/k3s-ssh.json")
    parser.add_argument("--cluster", type=Path, default=ROOT / "results/k3s-cluster")
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    topology = json.loads((args.cluster / "topology.json").read_text())
    for node, service in (("node1", "a-1"), ("node2", "a-2")):
        host = inventory[node]
        address = topology["services"]["service-a"]["instances"][service]
        override = json.dumps({"services": {service: {"environment": {"DISCOVERY_MODE": args.mode}}}})
        directory = f"/opt/loadbalance/cluster/{node}"
        compose = f"sudo docker compose -p lb-cloud -f {directory}/compose.json -f {directory}/mode.json"
        health = (
            "import grpc,time; from grpc_health.v1 import health_pb2 as p,health_pb2_grpc as r; "
            f"c=grpc.insecure_channel({address!r}); grpc.channel_ready_future(c).result(timeout=20); "
            "s=r.HealthStub(c); "
            "exec('for i in range(40):\\n try:\\n  assert s.Check(p.HealthCheckRequest(),timeout=1).status == 1; break"
            '\\n except (grpc.RpcError,AssertionError): time.sleep(.5)\\nelse: raise RuntimeError("not ready")\')'
        )
        command = (
            f"printf %s {shlex.quote(override)} | sudo tee {directory}/mode.json >/dev/null && "
            f"{compose} up -d --no-build --no-deps {service} && "
            f"{compose} exec -T {service} python -c {shlex.quote(health)}"
        )
        for attempt in range(3):
            result = subprocess.run(["ssh", "-F", str(args.ssh_config), host, command], timeout=90)
            if result.returncode == 0:
                break
            if result.returncode != 255 or attempt == 2:
                result.check_returncode()
            # The desired-mode write/up and readiness check are idempotent.
            time.sleep(1)


if __name__ == "__main__":
    main()
