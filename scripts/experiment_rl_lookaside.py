"""Closed-loop Q-learning experiment using real local gRPC, xDS and workload RPCs."""

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory

import grpc
from envoy.service.discovery.v3 import ads_pb2_grpc

from grpc_lb.controlplane.ads import AggregatedDiscoveryService, SnapshotStore
from grpc_lb.controlplane.application import SnapshotPublisher
from grpc_lb.controlplane.rl_controller import RLController
from grpc_lb.dependencies import XdsDependencyPool
from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.rl_telemetry import TelemetryCollector
from grpc_lb.service_runtime import start_service
from grpc_lb.topology import ServiceConfig


class DelayWorkload:
    def __init__(self, delay):
        self.delay = delay

    async def execute(self, request):
        await asyncio.sleep(self.delay)


async def experiment(
    windows,
    requests,
    seed,
    baseline=False,
    train_windows=0,
    model_path=None,
    slow="two",
    rule=False,
    concurrency=1,
    scenario="unequal",
    backend_capacity=50,
):
    config = ServiceConfig(
        "worker", {"one": "one:50051", "two": "two:50051"}, delay_s=0, concurrency=backend_capacity
    )
    runtimes = []
    server = grpc.aio.server()
    pool = None
    previous_bootstrap = os.environ.get("GRPC_XDS_BOOTSTRAP_CONFIG")
    temp = TemporaryDirectory()
    telemetry_path = Path(temp.name) / "telemetry.json"
    records = []
    workloads = {}
    try:
        for name in ("one", "two"):
            delay = 0.010 if scenario == "equal" else 0.030 if name == slow else 0.002
            workloads[name] = DelayWorkload(delay)
            runtimes.append(
                await start_service(config, name, {"worker": config}, "127.0.0.1:0", workload=workloads[name])
            )
        addresses = [f"127.0.0.1:{runtime.port}" for runtime in runtimes]

        class Source:
            async def read(self):
                return {"worker": addresses}

        store = SnapshotStore()
        controller = RLController(str(telemetry_path), "worker", seed=seed, model_path=model_path)
        if rule:

            def choose(state, *, explore=False, allowed=(-1, 0, 1)):
                action = 1 if state[0] == 0 else -1 if state[0] == 2 else 0
                return action if action in allowed else 0

            controller.agent.choose = choose
            controller.learning = False
        publisher = SnapshotPublisher(Source(), store, None if baseline else controller)
        await publisher.publish()
        ads_pb2_grpc.add_AggregatedDiscoveryServiceServicer_to_server(
            AggregatedDiscoveryService(store), server
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        os.environ["GRPC_XDS_BOOTSTRAP_CONFIG"] = json.dumps(
            {
                "xds_servers": [
                    {
                        "server_uri": f"127.0.0.1:{port}",
                        "channel_creds": [{"type": "insecure"}],
                        "server_features": ["xds_v3"],
                    }
                ],
                "node": {"id": "rl-lab"},
            }
        )
        collector = TelemetryCollector(
            "worker", dict(zip(("one", "two"), addresses, strict=True)), str(telemetry_path)
        )
        pool = XdsDependencyPool("worker", collector)
        await asyncio.wait_for(pool.channel.channel_ready(), 15)
        request = pb.TaskRequest(
            request_id="rl-lab", target_service="worker", workload="light", remaining_hops=4
        )

        async def measure():
            durations = defaultdict(list)
            errors = 0
            all_durations = []
            semaphore = asyncio.Semaphore(concurrency)

            async def call():
                nonlocal errors
                async with semaphore:
                    await invoke()

            async def invoke():
                nonlocal errors
                start = time.monotonic()
                try:
                    result = await pool.call(request, timeout=4)
                    elapsed = (time.monotonic() - start) * 1000
                    durations[result.instance_id].append(elapsed)
                    all_durations.append(elapsed)
                except grpc.aio.AioRpcError:
                    errors += 1

            await asyncio.gather(*(call() for _ in range(requests)))
            return durations, errors, all_durations

        durations, errors, times = await measure()
        for index in range(windows):
            if scenario == "swap" and index == windows // 2:
                workloads["one"].delay, workloads["two"].delay = (
                    workloads["two"].delay,
                    workloads["one"].delay,
                )
            if index == train_windows:
                controller.agent.epsilon = 0
                controller.learning = False
            await collector.flush()
            await publisher.publish()
            # Allow propagation before collecting a non-overlapping reward window.
            await asyncio.sleep(1)
            durations, errors, times = await measure()
            p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)] if times else 1000.0
            records.append(
                {
                    "window": index,
                    "phase": "train" if index < train_windows else "evaluation",
                    "weights_sorted_addresses": [controller.weight, 10 - controller.weight],
                    "counts": {k: len(v) for k, v in durations.items()},
                    "instance_median_ms": {k: statistics.median(v) for k, v in durations.items() if v},
                    "observed_category": controller.last_category,
                    "errors": errors,
                    "p95_ms": p95,
                    "latency_ms": times,
                    "offered": requests,
                }
            )
            if not baseline and not controller.members:
                records[-1]["fallback"] = "missing samples or errors"
                break
        return {
            "experiment": "real-grpc-local-q-learning",
            "baseline": baseline,
            "seed": seed,
            "slow": slow,
            "model_path": model_path,
            "rule": rule,
            "scenario": scenario,
            "concurrency": concurrency,
            "backend_capacity": backend_capacity,
            "records": records,
            "q": [{"state": list(k), "values": v} for k, v in sorted(controller.agent.q.items())],
        }
    finally:
        temp.cleanup()
        if pool:
            await pool.close()
        await server.stop(0)
        for runtime in runtimes:
            await runtime.close()
        if previous_bootstrap is None:
            os.environ.pop("GRPC_XDS_BOOTSTRAP_CONFIG", None)
        else:
            os.environ["GRPC_XDS_BOOTSTRAP_CONFIG"] = previous_bootstrap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=int, default=20)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline", action="store_true", help="Run unchanged round robin")
    parser.add_argument("--train-windows", type=int, default=0)
    parser.add_argument("--model")
    parser.add_argument("--slow", choices=("one", "two"), default="two")
    parser.add_argument("--rule", action="store_true")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--scenario", choices=("unequal", "equal", "swap"), default="unequal")
    parser.add_argument("--backend-capacity", type=int, default=50)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if (
        args.windows < 1
        or args.requests < 30
        or not 0 <= args.train_windows < args.windows
        or args.concurrency < 1
        or args.backend_capacity < 1
    ):
        parser.error("positive windows, >=30 requests, and 0 <= train-windows < windows required")
    result = asyncio.run(
        experiment(
            args.windows,
            args.requests,
            args.seed,
            args.baseline,
            args.train_windows,
            args.model,
            args.slow,
            args.rule,
            args.concurrency,
            args.scenario,
            args.backend_capacity,
        )
    )
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": args.output,
                "windows": len(result["records"]),
                "fallback": any("fallback" in r for r in result["records"]),
            }
        )
    )


if __name__ == "__main__":
    main()
