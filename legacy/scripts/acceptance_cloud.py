"""Acceptance over three real VMs; process faults and optional node3 power-off."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path

from acceptance_microservices import ROOT, traffic, wait_graph
from check_microservices import expectations
from multihost import PLACEMENT, SshDeployment

from grpc_lb.topology import load_topology


async def run(args):
    inventory = json.loads(args.inventory.read_text())
    if set(inventory) != set(PLACEMENT):
        raise ValueError("Three-node inventory required")
    ssh = SshDeployment(args.key, ROOT / "results/cloud-known-hosts", args.ssh_config)
    entry, topology = load_topology(args.cluster / "topology.json")
    expected = expectations(entry, topology)
    report = {
        "scope": "three Ubuntu VMs, same availability zone; physical-host separation unverified",
        "started_unix": time.time(),
        "passed": False,
        "stages": {},
        "release_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
    }
    stages = report["stages"]

    def save():
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    async def command(node, script):
        return await asyncio.to_thread(ssh.run, inventory[node], script)

    async def compose(node, operation):
        return await command(
            node,
            f"sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/{node}/compose.json {operation}\n",
        )

    async def graph(label, values=None):
        start = time.monotonic()
        counts = await wait_graph(args.address, entry, topology, values or expected)
        stages[label] = {"counts": counts, "converged_in_s": round(time.monotonic() - start, 3)}
        save()
        print(f"{label}: passed", flush=True)

    async def drain(enabled):
        expression = "p.touch()" if enabled else "p.unlink(missing_ok=True)"
        await compose(
            "node3",
            "exec -T b-2 python -c \"from pathlib import Path; p=Path('/tmp/drain'); " + expression + '"',
        )

    async def cold(label):
        output = await command(
            "node3",
            """sudo docker run --rm --network host \
  -v /opt/loadbalance/scripts:/app/scripts:ro \
  -v /opt/loadbalance/cluster/bootstrap.json:/app/configs/bootstrap.json:ro \
  -e GRPC_XDS_BOOTSTRAP=/app/configs/bootstrap.json \
  loadbalance-workload:cloud python /app/scripts/probe_xds.py
""",
        )
        result = json.loads(next(line for line in output.splitlines() if line.startswith('{"instances"')))
        if set(result["instances"]) != {"b-1", "b-2"}:
            raise RuntimeError("Cold xDS caller did not discover both B replicas")
        stages[label] = result
        save()

    async def peers(node="node1"):
        return json.loads(
            await command(node, "curl --fail --silent http://127.0.0.1:8500/v1/operator/raft/configuration\n")
        )

    reduced = copy.deepcopy(expected)
    reduced["service-b"] = {"b-1"}
    reduced_vm = copy.deepcopy(reduced)
    reduced_vm["service-c"] = {"c-1"}
    fault_node = None
    paused_nodes = []
    vm_powered_off = False
    try:
        identity = {}
        for node in inventory:
            output = await command(
                node,
                "hostname\ncat /proc/sys/kernel/random/boot_id\nsha256sum /opt/loadbalance/release.tar.gz\n",
            )
            lines = output.splitlines()
            if lines[2].split()[0] != report["release_sha256"]:
                raise RuntimeError(f"Release mismatch on {node}")
            identity[node] = {"hostname": lines[0], "boot_id": lines[1], "release": lines[2].split()[0]}
        if len({item["boot_id"] for item in identity.values()}) != 3:
            raise RuntimeError("Expected three independent VM kernels")
        stages["vm_identity"] = identity
        container_images = {}
        for node in inventory:
            listing = await compose(node, "ps -q")
            ids = listing.split()
            if not ids or any(not value.isalnum() for value in ids):
                raise RuntimeError(f"No valid running containers on {node}")
            output = await command(
                node, 'sudo docker inspect --format "{{.Name}} {{.Image}}" ' + " ".join(ids) + "\n"
            )
            container_images[node] = dict(line.split() for line in output.splitlines())
        workload_ids = {
            digest
            for rows in container_images.values()
            for name, digest in rows.items()
            if any(
                name.endswith(f"-{instance}-1")
                for instances in PLACEMENT.values()
                for _, instance, _ in instances
            )
        }
        if len(workload_ids) != 1:
            raise RuntimeError("Workload replicas are not running the identical distributed image")
        stages["running_container_images"] = container_images
        await graph("initial_graph")
        membership = await peers()
        if len(membership["Servers"]) != 3 or not all(row["Voter"] for row in membership["Servers"]):
            raise RuntimeError("Three Consul voters required")
        stages["initial_raft"] = membership
        for label, count, concurrency, workload in (
            ("baseline", 1000, 32, "light"),
            ("heavy", 300, 16, "heavy"),
            ("stress", 5000, 64, "light"),
        ):
            result = await traffic(args.address, entry, topology, count, concurrency, workload)
            stages[label] = result
            save()
            if result["codes"] != {"OK": count} or result["successful_after_client_timeout"]:
                raise RuntimeError(f"Load/deadline failure: {label}")
            print(f"{label}: passed", flush=True)
        await drain(True)
        await graph("not_serving_cross_vm", reduced)
        await drain(False)
        await graph("serving_recovery")
        for node in ("node1", "node2"):
            fault_node = node
            await compose(node, "stop controlplane")
            await drain(True)
            await graph(f"{node}_controlplane_down_new_update", reduced)
            await drain(False)
            await graph(f"{node}_controlplane_down_recovery")
            await cold(f"cold_caller_without_{node}_controlplane")
            await compose(node, "start controlplane")
            fault_node = None
        membership = await peers()
        leader = next(row["Node"] for row in membership["Servers"] if row["Leader"])
        leader_node = leader.removeprefix("lb-")
        fault_node = leader_node
        await compose(leader_node, "stop consul")
        await drain(True)
        await graph("consul_leader_loss_new_health", reduced)
        await compose(leader_node, "start consul")
        fault_node = None
        await drain(False)
        await graph("consul_leader_recovery")
        async with asyncio.timeout(90):
            while True:
                membership = await peers()
                if len(membership["Servers"]) == 3 and all(row["Voter"] for row in membership["Servers"]):
                    break
                await asyncio.sleep(2)
        stages["restored_raft"] = membership
        for node in ("node2", "node3"):
            await compose(node, "pause consul")
            paused_nodes.append(node)
        await asyncio.sleep(22)
        result = await traffic(args.address, entry, topology, 40, 4, warmup=False)
        stages["quorum_loss_expires_membership"] = result
        save()
        if result["codes"] != {"UNAVAILABLE": 40}:
            raise RuntimeError("Expired registry membership remained routable")
        for node in list(paused_nodes):
            await compose(node, "unpause consul")
            paused_nodes.remove(node)
        await graph("quorum_recovery")
        stages["short_deadlines"] = await traffic(
            args.address, entry, topology, 100, 8, timeout=0.001, warmup=False
        )
        if stages["short_deadlines"]["codes"] != {"DEADLINE_EXCEEDED": 100}:
            raise RuntimeError("Short deadline guard failed")
        await graph("after_deadline_recovery")
        if args.poweroff_node3:
            await command("node3", "sudo shutdown -h +1 'LoadBalance authorized VM fault acceptance'\n")
            report["operator_state"] = "node3_shutdown_scheduled"
            save()
            print("node3_shutdown_scheduled: waiting for power-off", flush=True)
            await asyncio.sleep(55)
            async with asyncio.timeout(90):
                while True:
                    try:
                        await command("node3", "true\n")
                    except RuntimeError:
                        break
                    await asyncio.sleep(3)
            vm_powered_off = True
            await graph("node3_vm_loss", reduced_vm)
            report["operator_state"] = "node3_off_verified_start_it_in_dashboard"
            save()
            print("node3_off_verified_start_it_in_dashboard", flush=True)
            async with asyncio.timeout(240):
                while True:
                    try:
                        boot = (await command("node3", "cat /proc/sys/kernel/random/boot_id\n")).strip()
                        if boot != identity["node3"]["boot_id"]:
                            break
                    except RuntimeError:
                        pass
                    await asyncio.sleep(3)
            vm_powered_off = False
            stages["node3_rebooted_boot_id"] = boot
            await graph("node3_vm_recovery")
        for node in ("node1", "node2"):
            logs = await compose(node, "logs --no-color controlplane")
            (ROOT / f"results/cloud-{node}-controlplane.log").write_text(logs, encoding="utf-8")
            if "xds_nack" in logs or "xds_ack" not in logs:
                raise RuntimeError(f"xDS evidence failed on {node}")
        report["passed"] = True
        report["operator_state"] = "completed"
    except BaseException as error:
        report["error"] = str(error)
        raise
    finally:
        report["vm3_may_be_powered_off"] = vm_powered_off
        for node in paused_nodes:
            try:
                await compose(node, "unpause consul")
            except Exception as error:
                report["paused_restore_error"] = str(error)
                report["passed"] = False
        if fault_node is not None:
            try:
                await compose(
                    fault_node, "start consul controlplane" if fault_node != "node3" else "start consul"
                )
            except Exception as error:
                report["fault_restore_error"] = str(error)
                report["passed"] = False
        try:
            if not vm_powered_off:
                # Idempotent cleanup can be retried after a transient SSH reset.
                for attempt in range(3):
                    try:
                        await drain(False)
                        break
                    except RuntimeError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(1)
        except Exception as error:
            report["drain_restore_error"] = str(error)
            report["passed"] = False
        report["finished_unix"] = time.time()
        report["resource_policy"] = (
            "retain the named three-VM lab; restore faults; do not delete cloud VMs/volumes"
        )
        save()
    if not report["passed"]:
        raise RuntimeError("Cloud acceptance or fault restoration failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--ssh-config", type=Path)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--cluster", type=Path, default=ROOT / "results/cloud-cluster")
    parser.add_argument("--archive", type=Path, default=ROOT / "results/loadbalance-release.tar.gz")
    parser.add_argument("--address", required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "results/cloud-acceptance.json")
    parser.add_argument(
        "--poweroff-node3", action="store_true", help="Schedule VM shutdown; dashboard start required"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
