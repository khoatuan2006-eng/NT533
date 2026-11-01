"""Measure live RPCs across a backend stop or a real VM1 stop/start.

VM test affects the shared k3s control-plane VM too. Run only in the authorized lab.
The client always uses the same Floating IP and one persistent gRPC channel.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import grpc
from cloud_api import Cloud
from controlled_probe import Probe, interruption, summarize

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc

ROOT = Path(__file__).resolve().parents[1]


async def run(args):
    await asyncio.to_thread(
        subprocess.run, [sys.executable, str(ROOT / "scripts/cloud_mode.py"), args.mode], check=True
    )
    cloud = Cloud(args.credentials) if args.fault == "vm1" else None
    vm = "/servers/50ffce03-5bfc-4137-90aa-a9960f8a09c7"
    if cloud and cloud.request("compute", "GET", vm)["server"]["status"] != "ACTIVE":
        raise RuntimeError("VM1 must be ACTIVE before fault injection")
    parameters = SimpleNamespace(
        address=args.address,
        output=args.output,
        mode=args.mode,
        entry="service-a",
        workload="light",
        rate=20,
        duration=120,
        timeout=3,
        ready_timeout=20,
        max_inflight=256,
        warmup=40,
        fault_at=None,
        stable_window=5,
    )
    probe = Probe(parameters)
    events = {"fault": args.fault, "mode": args.mode, "endpoint": args.address}
    task = None
    fault_attempted = False
    pin_disabled = False

    async def member2(enabled):
        await asyncio.to_thread(
            cloud.request,
            "load-balancer",
            "PUT",
            "/v2/lbaas/pools/cbf6cf17-84d0-485c-9277-be38a047ebe1/members/c35a8dbf-89ec-45bf-9a5b-29d28bc701fb",
            {"member": {"admin_state_up": enabled}},
        )
        await asyncio.to_thread(cloud.wait_lb, "62877bb9-c24d-4f24-8f57-cb6f4491ece7")

    async def backend(operation):
        command = (
            "sudo docker compose -p lb-cloud -f /opt/loadbalance/cluster/node3/compose.json "
            + operation
            + " b-2"
        )
        await asyncio.to_thread(
            subprocess.run,
            ["ssh", "-F", str(ROOT / "results/k3s-ssh-config"), "192.168.1.217", command],
            check=True,
            timeout=60,
        )

    async def vm_status(wanted):
        for _ in range(60):
            data = await asyncio.to_thread(cloud.request, "compute", "GET", vm)
            if data["server"]["status"] == wanted:
                return
            await asyncio.sleep(2)
        raise TimeoutError(f"VM1 did not become {wanted}")

    try:
        if args.prime_vm1:
            pin_disabled = True
            await member2(False)
            events["primed_existing_connection_through_vm1"] = True
        task = asyncio.create_task(probe.run())
        while not hasattr(probe, "origin"):
            if task.done():
                await task
                raise RuntimeError("Probe ended before measurement started")
            await asyncio.sleep(0.1)
        if pin_disabled:
            await member2(True)
            pin_disabled = False
        await asyncio.sleep(10)
        parameters.fault_at = time.perf_counter() - probe.origin
        events["fault_dispatch_s"] = parameters.fault_at
        fault_attempted = True
        if cloud:
            await asyncio.to_thread(cloud.request, "compute", "POST", vm + "/action", {"os-stop": None})
            await vm_status("SHUTOFF")
            events["confirmed_vm_status"] = "SHUTOFF"
        else:
            await backend("stop -t 0")
        events["fault_confirmed_s"] = time.perf_counter() - probe.origin
        await asyncio.sleep(20)
        if cloud:
            async with grpc.aio.insecure_channel(args.address) as cold:
                reply = await rpc.ProcessingServiceStub(cold).Execute(
                    pb.TaskRequest(
                        request_id="cold-while-vm1-off",
                        target_service="service-a",
                        workload="light",
                        remaining_hops=64,
                    ),
                    timeout=5,
                )
                events["cold_client_while_vm1_off"] = {
                    "request_id": reply.request_id,
                    "instance": reply.instance_id,
                    "children": [child.instance_id for child in reply.children],
                }
            events["ingress_status_while_vm1_off"] = await asyncio.to_thread(
                cloud.request,
                "load-balancer",
                "GET",
                "/v2/lbaas/loadbalancers/62877bb9-c24d-4f24-8f57-cb6f4491ece7/status",
            )
    finally:
        if pin_disabled:
            await member2(True)
        if fault_attempted:
            events["restore_dispatch_s"] = time.perf_counter() - probe.origin
            try:
                if cloud:
                    # Reconcile state after an ambiguous stop response before starting.
                    status = (await asyncio.to_thread(cloud.request, "compute", "GET", vm))["server"][
                        "status"
                    ]
                    if status != "ACTIVE":
                        await vm_status("SHUTOFF")
                        await asyncio.to_thread(
                            cloud.request, "compute", "POST", vm + "/action", {"os-start": None}
                        )
                    await vm_status("ACTIVE")
                else:
                    await backend("start")
                events["restore_confirmed_s"] = time.perf_counter() - probe.origin
            except Exception as exc:
                events["restore_error"] = str(exc)
                raise
            finally:
                args.output.with_suffix(".events.json").write_text(
                    json.dumps(events, indent=2), encoding="utf-8"
                )
        if task is not None:
            await task
    rows = [json.loads(line) for line in args.output.with_suffix(".jsonl").read_text().splitlines()]
    end = events["restore_dispatch_s"]
    during = [r for r in rows if r["scheduled_s"] < end]
    events["before_restore_summary"] = summarize(
        during, end, parameters.fault_at, 5, search_after=events["fault_confirmed_s"]
    )
    recovery = events["before_restore_summary"]["observed_recovery_s"]
    events["recovery_after_confirmation_s"] = (
        recovery - (events["fault_confirmed_s"] - parameters.fault_at) if recovery is not None else None
    )
    events["rpc_interruption"] = interruption(rows, parameters.fault_at, end)
    confirmed = [
        r for r in rows if events["fault_confirmed_s"] <= r.get("started_s", -1) < end and r["code"] == "OK"
    ]
    events["successes_while_fault_confirmed"] = len(confirmed)
    events["instances_while_fault_confirmed"] = sorted({i for r in confirmed for i in r["instances"]})
    args.output.with_suffix(".events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    print(json.dumps(events, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", choices=["backend", "vm1"], required=True)
    parser.add_argument("--mode", choices=["static", "xds"], required=True)
    parser.add_argument("--address", default="192.168.120.109:80")
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--prime-vm1",
        action="store_true",
        help="Prime connection through Envoy VM1 before restoring both pool members",
    )
    args = parser.parse_args()
    if args.fault == "vm1" and not args.credentials:
        parser.error("VM fault requires an external clouds.yaml credential")
    if args.prime_vm1 and args.fault != "vm1":
        parser.error("Priming is only supported for the VM1 fault")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
