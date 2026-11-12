"""Compare RL v2 with its strongest baselines on real local gRPC, before using the cluster.

One process hosts N backends (real admission queue: `concurrency` slots, `delay` per request),
a control plane serving xDS, and an open-loop Poisson load generator acting as service A.

Policies, all facing the same scenarios and the same offered load:
  rr          xDS, Round Robin (no controller)
  lr          static pool, Least Request (two random choices, per request)
  beta=<b>    xDS, weights from the version-2 formula with one fixed beta
  rl          xDS, the trained agent tuning beta (frozen model, no exploration)

The agent only adds value if it beats the best fixed beta; the report shows that gap and the
gap to Least Request per scenario. Path delay (network) is not reproducible in one process,
so slowness here is in processing; the cluster's slow-node3 covers network delay.

--backends N (default 3) keeps total capacity constant and lets about 30% of the backends
carry the fault. Run one comparison at a time: a run whose event loop falls behind by more
than 20 ms is marked invalid and left out of the tables, since its delay would read as latency.

    python scripts/compare_shape_rl.py --model results/shape-rl/model.json --output-dir results/shape-compare
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import grpc
from envoy.service.discovery.v3 import ads_pb2_grpc

from grpc_lb.controlplane.ads import AggregatedDiscoveryService, SnapshotStore
from grpc_lb.controlplane.application import SnapshotPublisher
from grpc_lb.controlplane.rl_controller import ShapeController
from grpc_lb.dependencies import StaticDependencyPool, XdsDependencyPool
from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.rl_telemetry import TelemetryCollector
from grpc_lb.service_runtime import start_service
from grpc_lb.topology import ServiceConfig

SERVICE = "worker"
BASE_DELAY = 0.05  # seconds per request at three backends; coarse Windows timers dislike less
# A run is invalid when the event loop itself falls behind: its delay then shows up as latency.
MAX_LOOP_LAG_MS = 20
# Also invalid when requests leave later than their Poisson schedule: the process stalled.
MAX_SEND_LATE_MS = 50
SCENARIOS = ("even", "processing-slow", "uneven-capacity", "flapping", "load-step")


def affected(n: int) -> int:
    """Backends a fault touches: about 30%, so a fault weighs the same at 3 and at 10 backends."""
    return max(1, round(0.3 * n))


def base_delay(n: int) -> float:
    """Per-request delay that keeps total capacity constant (4 slots each) whatever n is."""
    return BASE_DELAY * n / 3


def scenario_specs(scenario: str, n: int) -> list[tuple[float, int]]:
    """(delay seconds, concurrency slots) per backend; at n=3 these are the original scenarios."""
    delay, k = base_delay(n), affected(n)
    specs = [(delay, 4)] * n
    if scenario in ("processing-slow", "flapping"):
        specs = specs[: n - k] + [(delay * 3, 4)] * k
    elif scenario == "uneven-capacity":
        specs = [(delay, 8)] * k + [(delay, 4)] * (n - 2 * k) + [(delay, 2)] * k
    elif scenario == "load-step":
        specs = specs[: n - k] + [(delay, 2)] * k
    return specs


class DelayWorkload:
    def __init__(self, delay: float):
        self.delay = delay

    async def execute(self, request):
        await asyncio.sleep(self.delay)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


async def run_one(policy: str, scenario: str, args, seed: int) -> dict:
    specs = scenario_specs(scenario, args.backends)
    capacity = sum(slots / delay for delay, slots in specs)
    names = [f"w{i + 1}" for i in range(len(specs))]
    workloads = {name: DelayWorkload(delay) for name, (delay, _) in zip(names, specs, strict=True)}
    runtimes, pool, server, publisher_task = [], None, None, None
    temp = TemporaryDirectory()
    previous_bootstrap = os.environ.get("GRPC_XDS_BOOTSTRAP_CONFIG")
    try:
        for name, (_, slots) in zip(names, specs, strict=True):
            config = ServiceConfig(SERVICE, {name: f"{name}:50051"}, delay_s=0, concurrency=slots)
            runtimes.append(
                await start_service(config, name, {SERVICE: config}, "127.0.0.1:0", workload=workloads[name])
            )
        addresses = [f"127.0.0.1:{runtime.port}" for runtime in runtimes]
        telemetry = Path(temp.name) / "telemetry.json"
        collector = TelemetryCollector(
            SERVICE, dict(zip(names, addresses, strict=True)), str(telemetry), latency="rpc"
        )
        if policy == "lr":
            pool = StaticDependencyPool(SERVICE, addresses, policy="least_request")
            await pool.start()
        else:
            controller = None
            if policy == "rl":
                controller = ShapeController(
                    str(telemetry), SERVICE, policy="shape-q", model_path=args.model, max_age=args.window * 2
                )
            elif policy.startswith("beta="):
                controller = ShapeController(
                    str(telemetry),
                    SERVICE,
                    policy="shape-fixed",
                    beta=float(policy.split("=", 1)[1]),
                    max_age=args.window * 2,
                )

            class Source:
                async def read(self):
                    return {SERVICE: addresses}

            store = SnapshotStore()
            publisher = SnapshotPublisher(Source(), store, controller)
            await publisher.publish()
            server = grpc.aio.server()
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
                    "node": {"id": "shape-compare"},
                }
            )
            pool = XdsDependencyPool(SERVICE, collector)
            await asyncio.wait_for(pool.channel.channel_ready(), 15)

            async def control_loop():
                # The collector's own timer, made explicit: one window, then publish.
                while True:
                    await asyncio.sleep(args.window)
                    await collector.flush()
                    await publisher.publish()

            publisher_task = asyncio.create_task(control_loop())

        lags: list[float] = []

        async def watch_loop():
            # How late a 10 ms sleep wakes up: the harness's own delay, not the system's.
            while True:
                start = time.monotonic()
                await asyncio.sleep(0.01)
                lags.append(max(0.0, (time.monotonic() - start - 0.01) * 1000))

        lag_task = asyncio.create_task(watch_loop())
        late: list[float] = []
        rng = random.Random(seed)
        records: list[tuple[int, float, bool, str]] = []
        total_windows = args.warmup + args.windows
        in_flight: set[asyncio.Task] = set()

        async def call(window: int, index: int):
            request = pb.TaskRequest(
                request_id=f"{policy}-{index}", target_service=SERVICE, workload="light", remaining_hops=4
            )
            start = time.monotonic()
            try:
                result = await pool.call(request, timeout=args.timeout)
                records.append((window, (time.monotonic() - start) * 1000, True, result.instance_id))
            except Exception:  # every failure (RPC error, no backend, timeout) counts the same
                records.append((window, (time.monotonic() - start) * 1000, False, ""))

        def utilization_at(window: int) -> float:
            if scenario == "load-step":
                return args.utilization * (0.5 if window < args.warmup + args.windows // 2 else 1.15)
            return args.utilization

        # Arrivals follow an absolute Poisson schedule: coarse timers then delay a batch of
        # requests instead of silently lowering the offered rate.
        begin, index = time.monotonic(), 0
        arrival = 0.0
        while arrival < total_windows * args.window:
            elapsed = time.monotonic() - begin
            while arrival <= elapsed and arrival < total_windows * args.window:
                late.append((elapsed - arrival) * 1000)
                window = int(arrival // args.window)
                if scenario == "flapping":
                    for name in names[-affected(len(names)) :]:
                        workloads[name].delay = base_delay(len(names)) * (3 if (window // 3) % 2 == 0 else 1)
                if len(in_flight) < args.max_inflight:
                    task = asyncio.create_task(call(window, index))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                else:
                    records.append((window, 0.0, False, "generator-overflow"))
                index += 1
                arrival += rng.expovariate(utilization_at(window) * capacity)
            await asyncio.sleep(max(0.0, arrival - (time.monotonic() - begin)))
        await asyncio.gather(*in_flight, return_exceptions=True)
        lag_task.cancel()
        await asyncio.gather(lag_task, return_exceptions=True)
        lag_p99 = percentile(lags, 0.99) or 0.0
        late_p99 = percentile(late, 0.99) or 0.0
        measured = [r for r in records if r[0] >= args.warmup]
        ok = [r[1] for r in measured if r[2]]
        served = [r[3] for r in measured if r[2]]
        return {
            "policy": policy,
            "scenario": scenario,
            "seed": seed,
            "requests": len(measured),
            "error_percent": 100 * (len(measured) - len(ok)) / max(1, len(measured)),
            "mean_ms": statistics.mean(ok) if ok else None,
            "p50_ms": percentile(ok, 0.50),
            "p95_ms": percentile(ok, 0.95),
            "p99_ms": percentile(ok, 0.99),
            "share_percent": {name: 100 * served.count(name) / max(1, len(served)) for name in names},
            "loop_lag_p99_ms": lag_p99,
            "send_late_p99_ms": late_p99,
            "valid": lag_p99 <= MAX_LOOP_LAG_MS and late_p99 <= MAX_SEND_LATE_MS,
        }
    finally:
        if publisher_task:
            publisher_task.cancel()
            await asyncio.gather(publisher_task, return_exceptions=True)
        if pool:
            await pool.close()
        if server:
            await server.stop(0)
        for runtime in runtimes:
            await runtime.close()
        if previous_bootstrap is None:
            os.environ.pop("GRPC_XDS_BOOTSTRAP_CONFIG", None)
        else:
            os.environ["GRPC_XDS_BOOTSTRAP_CONFIG"] = previous_bootstrap
        temp.cleanup()


def spread(values) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "—"
    return f"{statistics.median(values):.1f} [{min(values):.1f}–{max(values):.1f}]"


def report(rows: list[dict], args) -> str:
    lines = [
        "# RL v2 trên localhost",
        "",
        f"{len(args.scenarios)} kịch bản, {args.backends} backend, tải mở ở {args.utilization:.0%} năng lực, cửa sổ "
        f"{args.window:g} giây, {args.warmup} cửa sổ khởi động không đo + {args.windows} cửa sổ đo, "
        f"{args.repetitions} lần lặp (thứ tự chính sách xáo theo lần lặp). Ô là median [min–max] giữa các lần.",
        "",
        "| Kịch bản | Chính sách | Mean (ms) | P95 (ms) | P99 (ms) | Lỗi (%) | Gửi trễ lịch P99 (ms) |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    invalid = [row for row in rows if not row.get("valid", True)]
    rows = [row for row in rows if row.get("valid", True)]
    cells: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        cells.setdefault((row["scenario"], row["policy"]), []).append(row)
    for (scenario, policy), group in sorted(cells.items()):
        lines.append(
            f"| {scenario} | {policy} | {spread(r['mean_ms'] for r in group)} | "
            f"{spread(r['p95_ms'] for r in group)} | {spread(r['p99_ms'] for r in group)} | "
            f"{spread(r['error_percent'] for r in group)} | {spread(r.get('send_late_p99_ms') for r in group)} |"
        )
    if invalid:
        lines += [
            "",
            f"**{len(invalid)} lượt bị loại** vì máy đo bị nghẽn (event loop trễ quá {MAX_LOOP_LAG_MS} ms hoặc request gửi trễ lịch quá {MAX_SEND_LATE_MS} ms; "
            "số latency không tin được): "
            + ", ".join(f"{r['scenario']}/{r['policy']}" for r in invalid)
            + ". Chạy lại khi máy rảnh, không chạy hai lượt song song.",
        ]
    lines += [
        "",
        "## RL so với đối thủ mạnh nhất",
        "",
        "Hiệu theo từng lần lặp (cùng lần lặp, cùng tải), median [min–max]; số âm là RL nhanh hơn. "
        "β cố định tốt nhất chọn một lần cho mọi kịch bản (mean thấp nhất tính chung), như khi triển khai thật.",
        "",
    ]
    fixed = sorted({row["policy"] for row in rows if row["policy"].startswith("beta=")})
    if "rl" in {row["policy"] for row in rows} and fixed:

        def overall(policy):
            return statistics.mean(
                r["mean_ms"] for r in rows if r["policy"] == policy and r["mean_ms"] is not None
            )

        best = min(fixed, key=overall)
        lines += [
            f"β cố định tốt nhất: `{best}`.",
            "",
            "| Kịch bản | RL − " + best + " mean (ms) | RL − lr mean (ms) | RL − rr mean (ms) |",
            "|---|---:|---:|---:|",
        ]
        for scenario in args.scenarios:

            def gaps(other):
                by_seed = {
                    r["seed"]: r["mean_ms"]
                    for r in rows
                    if r["scenario"] == scenario and r["policy"] == other
                }
                return [
                    r["mean_ms"] - by_seed[r["seed"]]
                    for r in rows
                    if r["scenario"] == scenario
                    and r["policy"] == "rl"
                    and r["mean_ms"] is not None
                    and by_seed.get(r["seed"]) is not None
                ]

            lines.append(
                f"| {scenario} | {spread(gaps(best))} | {spread(gaps('lr'))} | {spread(gaps('rr'))} |"
            )
    lines += [
        "",
        "Kết luận RL tốt hơn một đối thủ ở một kịch bản chỉ khi hiệu âm ở mọi lần lặp. "
        "Một tiến trình chạy cả backend lẫn bộ phát tải: so tương đối, không lấy số tuyệt đối.",
        "",
    ]
    return "\n".join(lines)


async def suite(args) -> list[dict]:
    rows = []
    policies = ["rr", "lr", *[f"beta={b:g}" for b in args.betas], *(["rl"] if args.model else [])]
    if args.policies:
        unknown = set(args.policies) - set(policies)
        if unknown:
            raise SystemExit(
                f"unknown policies: {', '.join(sorted(unknown))}; choose from {', '.join(policies)}"
            )
        policies = [p for p in policies if p in args.policies]
    for rep in range(args.repetitions):
        order = list(policies)
        random.Random(f"{args.seed}-{rep}").shuffle(order)
        for scenario in args.scenarios:
            for policy in order:
                print(f"lần {rep + 1}/{args.repetitions} {scenario} {policy}", flush=True)
                row = await run_one(policy, scenario, args, seed=args.seed * 1000 + rep)
                rows.append(row)
                with (args.output_dir / "runs.jsonl").open("a", encoding="utf-8") as runs:
                    runs.write(json.dumps(row) + "\n")
    return rows


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", help="Trained shape model; omit to run only the baselines")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--backends", type=int, default=3, help="Backends behind the caller (2..12)")
    parser.add_argument("--betas", nargs="+", type=float, default=[0.5, 1, 2, 4])
    parser.add_argument("--utilization", type=float, default=0.7, help="Offered load / total capacity")
    parser.add_argument("--window", type=float, default=5, help="Telemetry window, seconds")
    parser.add_argument("--warmup", type=int, default=4, help="Unmeasured windows before measuring")
    parser.add_argument("--windows", type=int, default=8, help="Measured windows")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=2)
    parser.add_argument("--max-inflight", type=int, default=400)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--log-decisions", action="store_true", help="Write every controller decision to decisions.log"
    )
    parser.add_argument("--policies", nargs="+", help="Run only these policies (default: all)")
    args = parser.parse_args()
    if not 2 <= args.backends <= 12:
        parser.error("--backends must be in 2..12")
    if not 0 < args.utilization < 1.5 or args.window < 1 or args.windows < 2 or args.warmup < 2:
        parser.error("0 < utilization < 1.5, window >= 1 s, warmup >= 2 and windows >= 2 required")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.log_decisions:
        handler = logging.FileHandler(args.output_dir / "decisions.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        controller_log = logging.getLogger("grpc_lb.controlplane.rl_controller")
        controller_log.setLevel(logging.INFO)
        controller_log.addHandler(handler)
    rows = asyncio.run(suite(args))
    text = report(rows, args)
    (args.output_dir / "report.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
