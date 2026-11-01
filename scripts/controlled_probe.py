"""Record every RPC at a fixed offered rate; summarize without hiding failures."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
import uuid
from collections import Counter
from pathlib import Path

import grpc

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc


def percentile(values, percent):
    """Nearest-rank percentile; missing samples remain null, never zero."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)] if ordered else None


def interruption(rows, fault_at, restore_at, stable_window=5):
    """RPC-visible interruption, independent of operator/API response latency."""
    phase = [r for r in rows if fault_at <= r["scheduled_s"] < restore_at]
    if not phase:
        return {"first_error_s": None, "last_error_s": None, "rpc_interruption_s": None}
    failures = [r for r in phase if r["code"] != "OK"]
    if not failures:
        return {"first_error_s": None, "last_error_s": None, "rpc_interruption_s": 0}
    first = min(r["scheduled_s"] for r in failures)
    last = max(r["scheduled_s"] for r in failures)
    result = {"first_error_s": first, "last_error_s": last, "rpc_interruption_s": None}
    for row in sorted(phase, key=lambda r: r["scheduled_s"]):
        start = row["scheduled_s"]
        if start > last and start + stable_window <= restore_at:
            result["rpc_interruption_s"] = start - first
            break
    return result


def summarize(rows, duration, fault_at=None, stable_window=5, search_after=None):
    calls = [r for r in rows if r["code"] != "GENERATOR_OVERFLOW"]
    successes = [r for r in calls if r["code"] == "OK"]
    elapsed = max([duration, *[r.get("completed_s", 0) for r in calls]])
    codes = Counter(r["code"] for r in rows)
    recovery = None
    # Require a complete post-fault interval with requests and no errors/drops.
    # This is observed service recovery, not the cloud health-monitor transition.
    if fault_at is not None:
        for row in sorted(rows, key=lambda r: r["scheduled_s"]):
            start = row["scheduled_s"]
            end = start + stable_window
            if (
                start < max(fault_at, search_after if search_after is not None else fault_at)
                or end > duration
            ):
                continue
            window = [r for r in rows if start <= r["scheduled_s"] <= end]
            if window and all(r["code"] == "OK" for r in window):
                recovery = start - fault_at
                break
    return {
        "scheduled": len(rows),
        "attempted": len(calls),
        "successful": len(successes),
        "codes": dict(codes),
        "successful_rps_offered_window": len(successes) / duration,
        "successful_rps_elapsed": len(successes) / elapsed,
        "failure_fraction_including_generator_drops": 1 - len(successes) / len(rows) if rows else None,
        "rpc_error_fraction": 1 - len(successes) / len(calls) if calls else None,
        "valid_load": not codes.get("GENERATOR_OVERFLOW", 0),
        "success_latency_ms": {
            f"p{p}": percentile([r["latency_ms"] for r in successes], p) for p in (50, 95, 99)
        },
        "all_rpc_latency_ms": {
            f"p{p}": percentile([r["latency_ms"] for r in calls], p) for p in (50, 95, 99)
        },
        "schedule_lag_ms_p99": percentile([r["schedule_lag_ms"] for r in calls], 99),
        "observed_recovery_s": recovery,
        "recovery_stable_window_s": stable_window,
    }


class Probe:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex

    async def run(self):
        args = self.args
        rows = []
        active = set()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # One persistent channel deliberately exercises reconnection on ingress loss.
        async with grpc.aio.insecure_channel(args.address) as channel:
            stub = rpc.ProcessingServiceStub(channel)
            await asyncio.wait_for(channel.channel_ready(), args.ready_timeout)

            def request(index):
                return pb.TaskRequest(
                    request_id=f"{self.run_id}-{index}",
                    target_service=args.entry,
                    workload=args.workload,
                    remaining_hops=64,
                )

            for index in range(args.warmup):
                await stub.Execute(request(f"warmup-{index}"), timeout=args.timeout)
            epoch = time.time()
            origin = time.perf_counter()
            self.started_unix_s = epoch
            self.origin = origin
            raw_path = args.output.with_suffix(".jsonl")
            with raw_path.open("x", encoding="utf-8") as raw:

                def record(row):
                    rows.append(row)
                    raw.write(json.dumps(row) + "\n")
                    raw.flush()

                async def call(index, scheduled):
                    started = time.perf_counter()
                    row = {
                        "index": index,
                        "scheduled_s": scheduled,
                        "started_s": started - origin,
                        "schedule_lag_ms": max(0, started - origin - scheduled) * 1000,
                    }
                    try:
                        result = await stub.Execute(request(index), timeout=args.timeout)
                        if result.request_id != request(index).request_id or result.service_id != args.entry:
                            row["code"] = "INVALID_RESULT"
                        else:
                            row["code"] = "OK"

                        def identities(node):
                            return [node.instance_id] + [v for c in node.children for v in identities(c)]

                        row["instances"] = identities(result)
                    except grpc.aio.AioRpcError as exc:
                        row["code"] = exc.code().name
                    row["latency_ms"] = (time.perf_counter() - started) * 1000
                    row["completed_s"] = time.perf_counter() - origin
                    record(row)

                for index in range(math.ceil(args.rate * args.duration)):
                    scheduled = index / args.rate
                    # asyncio timers may fire up to one clock resolution early
                    # on Windows; never dispatch before the intended arrival.
                    while (remaining := origin + scheduled - time.perf_counter()) > 0:
                        await asyncio.sleep(remaining)
                    if len(active) >= args.max_inflight:
                        record({"index": index, "scheduled_s": scheduled, "code": "GENERATOR_OVERFLOW"})
                        continue
                    task = asyncio.create_task(call(index, scheduled))
                    active.add(task)
                    task.add_done_callback(active.discard)
                if active:
                    await asyncio.gather(*active)
            report = {
                "schema_version": 2,
                "run_id": self.run_id,
                "started_unix_s": epoch,
                "python": platform.python_version(),
                "grpc": grpc.__version__,
                "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                "completion_elapsed_s": time.perf_counter() - origin,
                "raw": str(raw_path),
                "summary": summarize(rows, args.duration, args.fault_at, args.stable_window),
            }
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report["summary"], indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--mode", required=True, choices=["static", "xds"], help="Label; does not change services"
    )
    parser.add_argument("--entry", default="service-a")
    parser.add_argument("--workload", choices=["light", "heavy"], default="light")
    parser.add_argument("--rate", type=float, default=50)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--ready-timeout", type=float, default=20)
    parser.add_argument("--max-inflight", type=int, default=256)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument(
        "--fault-at", type=float, help="Actual fault offset from started_unix_s, not planned offset"
    )
    parser.add_argument("--stable-window", type=float, default=5)
    args = parser.parse_args()
    if (
        min(args.rate, args.duration, args.timeout, args.ready_timeout, args.max_inflight, args.stable_window)
        <= 0
    ):
        parser.error("Rates, durations, deadlines and capacity must be positive")
    if args.warmup < 0 or (args.fault_at is not None and not 0 <= args.fault_at < args.duration):
        parser.error("Invalid warmup or fault offset")
    asyncio.run(Probe(args).run())


if __name__ == "__main__":
    main()
