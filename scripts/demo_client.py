"""Demo client: send a few requests per second and show who served each one and how long it took.

Each laptop in a demo runs one copy. Every line shows the request number, the Envoy it went
through, the A, B and C instances that served it, and its end-to-end time. A summary every
few seconds shows the latency percentiles and how the requests were spread over instances,
so stopping a node or slowing a backend is visible as it happens.

    python scripts/demo_client.py --targets 3.25.161.150:80 16.179.93.25:80 --name laptop-khoa
    python scripts/demo_client.py --targets 3.25.161.150:80 --rate 10 --duration 120

Ctrl+C stops it and prints the final summary. Not a measurement tool: it shares the laptop and
the Internet path with everything else, so use the numbers to watch, not to report.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grpc_lb.generated import processing_pb2 as pb  # noqa: E402
from grpc_lb.generated import processing_pb2_grpc as rpc  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


@dataclass
class Outcome:
    seq: int
    target: str
    code: str
    latency_ms: float
    instances: list[str] = field(default_factory=list)


def served_by(node) -> list[str]:
    """Instance names in call order: a-x, then the B and C instances it called."""
    return [node.instance_id] + [name for child in node.children for name in served_by(child)]


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q / 100 * (len(ordered) - 1))))]


class Stats:
    def __init__(self) -> None:
        self.outcomes: list[Outcome] = []

    def add(self, outcome: Outcome) -> None:
        self.outcomes.append(outcome)

    def summary(self, since: int = 0) -> str:
        """Percentiles over successful requests and the share of each instance per tier."""
        window = self.outcomes[since:]
        if not window:
            return "chưa có request"
        ok = [o for o in window if o.code == "OK"]
        latencies = [o.latency_ms for o in ok]
        errors = collections.Counter(o.code for o in window if o.code != "OK")
        lines = [
            f"{len(window)} request, {len(ok)} thành công, {len(window) - len(ok)} lỗi"
            + (f" ({', '.join(f'{c} {n}' for c, n in errors.most_common())})" if errors else ""),
        ]
        if latencies:
            lines.append(
                f"độ trễ ms: P50 {percentile(latencies, 50):.0f}  P90 {percentile(latencies, 90):.0f}  "
                f"P99 {percentile(latencies, 99):.0f}  trung bình {statistics.fmean(latencies):.0f}"
            )
        tiers: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for o in ok:
            for name in o.instances:
                tiers[name.split("-")[0].upper()][name] += 1
        for tier in sorted(tiers):
            total = sum(tiers[tier].values())
            share = "  ".join(f"{n} {c / total:.0%}" for n, c in sorted(tiers[tier].items()))
            lines.append(f"tầng {tier}: {share}")
        return "\n".join(lines)


def line(outcome: Outcome, client: str, slow_ms: float) -> str:
    stamp = time.strftime("%H:%M:%S")
    if outcome.code != "OK":
        color = RED
        detail = outcome.code
    else:
        color = YELLOW if outcome.latency_ms >= slow_ms else GREEN
        a, *rest = outcome.instances or ["?"]
        detail = f"{a} → {', '.join(rest)}"
    return (
        f"{DIM}{stamp} {client} #{outcome.seq:<5} qua {outcome.target:<21}{RESET} "
        f"{color}{outcome.latency_ms:7.1f} ms  {detail}{RESET}"
    )


async def call(stub, target: str, seq: int, client: str, timeout: float) -> Outcome:
    request = pb.TaskRequest(
        request_id=f"{client}-{seq}", target_service="service-a", workload="light", remaining_hops=64
    )
    start = time.perf_counter()
    try:
        result = await stub.Execute(request, timeout=timeout)
        return Outcome(seq, target, "OK", (time.perf_counter() - start) * 1000, served_by(result))
    except grpc.aio.AioRpcError as error:
        return Outcome(seq, target, error.code().name, (time.perf_counter() - start) * 1000)


async def run(args, stats: Stats, out=print) -> None:
    channels = [grpc.aio.insecure_channel(t) for t in args.targets]
    stubs = [rpc.ProcessingServiceStub(c) for c in channels]
    interval = 1 / args.rate
    start = time.monotonic()
    pending: set[asyncio.Task] = set()
    printed_until, next_summary = 0, start + args.summary_every

    def done(task: asyncio.Task) -> None:
        pending.discard(task)
        outcome = task.result()
        stats.add(outcome)
        if not args.quiet:
            out(line(outcome, args.name, args.slow_ms))

    try:
        seq = 0
        while not args.duration or time.monotonic() - start < args.duration:
            seq += 1
            # Alternate Envoys request by request, as a client-side DNS round robin would.
            i = (seq - 1) % len(stubs)
            task = asyncio.create_task(call(stubs[i], args.targets[i], seq, args.name, args.timeout))
            task.add_done_callback(done)
            pending.add(task)
            if time.monotonic() >= next_summary:
                out(f"\n--- {args.summary_every:g} giây qua ---\n{stats.summary(printed_until)}\n")
                printed_until, next_summary = len(stats.outcomes), next_summary + args.summary_every
            # Fixed schedule: a slow reply does not delay the next request.
            await asyncio.sleep(max(0.0, start + seq * interval - time.monotonic()))
    finally:
        if pending:
            await asyncio.wait(pending, timeout=args.timeout + 1)
        for channel in channels:
            await channel.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--targets", nargs="+", required=True, help="Envoy host:port; requests alternate between them"
    )
    parser.add_argument(
        "--name", default=os.environ.get("COMPUTERNAME", "client"), help="Label printed on every line"
    )
    parser.add_argument(
        "--rate", type=float, default=5, help="Requests per second (keep it small over the Internet)"
    )
    parser.add_argument("--duration", type=float, default=0, help="Seconds to run; 0 runs until Ctrl+C")
    parser.add_argument("--timeout", type=float, default=3, help="Per-request deadline in seconds")
    parser.add_argument("--summary-every", type=float, default=10, help="Seconds between summaries")
    parser.add_argument(
        "--slow-ms", type=float, default=0, help="Show replies slower than this in yellow (0: auto)"
    )
    parser.add_argument("--quiet", action="store_true", help="Only print the summaries")
    args = parser.parse_args()
    if not 0 < args.rate <= 50 or args.timeout <= 0 or args.summary_every <= 0 or args.duration < 0:
        parser.error("0 < rate <= 50, positive timeout and summary interval, duration >= 0")
    if sys.platform == "win32":
        os.system("")  # turn on ANSI colours in the Windows console
    stats = Stats()
    if not args.slow_ms:
        # Over the Internet the base latency is the round trip; mark replies well above it.
        args.slow_ms = 1e9
    try:
        asyncio.run(_auto_then_run(args, stats))
    except KeyboardInterrupt:
        pass
    print(f"\n=== Tổng kết {args.name} ===\n{stats.summary()}")


async def _auto_then_run(args, stats: Stats) -> None:
    if args.slow_ms == 1e9:
        # Ten warm-up requests set the "slow" threshold at twice their median latency.
        probe = Stats()
        quiet = argparse.Namespace(
            **{**vars(args), "duration": 10 / args.rate, "quiet": True, "summary_every": 1e9}
        )
        await run(quiet, probe, out=lambda _: None)
        ok = [o.latency_ms for o in probe.outcomes if o.code == "OK"]
        args.slow_ms = 2 * statistics.median(ok) if ok else 200
        print(f"Ngưỡng tô vàng: {args.slow_ms:.0f} ms (gấp đôi trung vị 10 request đầu)\n")
    await run(args, stats)


if __name__ == "__main__":
    main()
