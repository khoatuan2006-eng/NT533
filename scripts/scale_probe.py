"""High-rate open-loop load generator for capacity runs: many processes, many connections.

controlled_probe.py records every RPC on one connection, which is right for fault runs but
tops out near a few thousand RPS and pins all traffic to one Envoy worker. This tool spreads
the offered rate over worker processes, and each worker over several HTTP/2 connections
spread across every ingress address, so neither the generator nor one connection is the
bottleneck. It keeps aggregates (codes, latencies, per-instance counts), not one row per RPC.

    python scripts/scale_probe.py --targets 10.0.1.10:80 10.0.1.11:80 \\
        --rates 2000 4000 8000 12000 16700 --duration 60 --output results/scale/<run>

Steps run in order and stop at the first step that fails the pass criteria (--keep-going to
continue). A step is valid only if the generator itself kept its schedule; an invalid step
says nothing about the cluster, so add workers or a bigger load machine and repeat it.

When one machine cannot offer the load, run the same command on several machines with each
machine's share of the rates and one common --start-at, then combine:
    python scripts/scale_probe.py merge --inputs run-m1 run-m2 --output run-all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import multiprocessing as mp
import os
import platform
import sys
import time
import uuid
from array import array
from collections import Counter
from pathlib import Path

# Same pre-registered pass criteria as run_load.py.
MAX_ERROR_FRACTION = 0.01
MIN_GOODPUT_FRACTION = 0.98
MAX_P99_MS = 3000
# The generator is trusted only while it dispatches close to schedule.
MAX_SCHEDULE_LAG_P99_MS = 50
DISPATCH_TICK_S = 0.001


def percentile(ordered, percent):
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)] if ordered else None


def _channel_options(index: int):
    # A local subchannel pool plus a distinct argument gives each channel its own TCP
    # connection; otherwise gRPC would share one connection per target in the process.
    return [("grpc.use_local_subchannel_pool", 1), ("lb.channel_index", index)]


async def _worker_main(config: dict) -> dict:
    import grpc

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from grpc_lb.generated import processing_pb2 as pb
    from grpc_lb.generated import processing_pb2_grpc as rpc

    targets, worker, workers = config["targets"], config["worker"], config["workers"]
    channels = [
        grpc.aio.insecure_channel(targets[(worker + i * workers) % len(targets)], options=_channel_options(i))
        for i in range(config["channels"])
    ]
    stubs = [rpc.ProcessingServiceStub(channel) for channel in channels]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(channel.channel_ready() for channel in channels)), config["ready_timeout"]
        )
    except TimeoutError:
        for channel in channels:
            await channel.close()
        return {"worker": worker, "error": "channels not ready (check --targets and the ingress)"}

    from load_profile import Phase, Profile
    from load_profile import parse as parse_profile

    warmup = config["warmup"]
    profile = parse_profile(config["profile"]) if config.get("profile") else None
    if profile is not None:
        # Warm up at the first phase's rate, then follow the profile; only the profile is measured.
        first = profile.phases[0].start_rate or profile.mean_rate
        lead = (Phase(first, first, warmup),) if warmup else ()
        arrivals = Profile(profile.name, profile.spec, lead + profile.phases).arrivals()
    else:
        total_rate = config["rate"]
        arrivals = (k / total_rate for k in range(math.ceil(total_rate * (warmup + config["duration"]))))
    # Interleave the workers: worker w sends arrivals w, w + W, w + 2W, ...
    schedule = array("d", (t for k, t in enumerate(arrivals) if k % workers == worker))
    phase_count = len(profile.phases) if profile else 1
    timeout, entry, workload = config["timeout"], config["entry"], config["workload"]
    prefix = f"{config['run_id']}-{config['step']}-{worker}"
    latencies, lags = array("f"), array("f")
    codes, instances, per_second = Counter(), Counter(), Counter()
    phase_latencies = [array("f") for _ in range(phase_count)]
    phase_codes = [Counter() for _ in range(phase_count)]
    phase_instances = [Counter() for _ in range(phase_count)]
    active: set[asyncio.Task] = set()
    overflow = 0

    def identities(node):
        yield node.instance_id
        for child in node.children:
            yield from identities(child)

    async def call(index: int, scheduled: float, measured: bool):
        started = time.perf_counter()
        request = pb.TaskRequest(
            request_id=f"{prefix}-{index}", target_service=entry, workload=workload, remaining_hops=64
        )
        try:
            result = await stubs[index % len(stubs)].Execute(request, timeout=timeout)
            code = "OK" if result.request_id == request.request_id else "INVALID_RESULT"
        except grpc.aio.AioRpcError as error:
            code, result = error.code().name, None
        if not measured:
            return
        latency = (time.perf_counter() - started) * 1000
        codes[code] += 1
        offset = scheduled - warmup
        per_second[(int(offset), code == "OK")] += 1
        phase = profile.phase_at(offset) if profile else 0
        phase_codes[phase][code] += 1
        if code == "OK":
            latencies.append(latency)
            names = list(identities(result))
            instances.update(names)
            phase_latencies[phase].append(latency)
            phase_instances[phase].update(names)

    origin = time.perf_counter() + config["start_delay"]
    index, total = 0, len(schedule)
    while index < total:
        now = time.perf_counter() - origin
        while index < total and (scheduled := schedule[index]) <= now:
            measured = scheduled >= warmup
            if len(active) >= config["max_inflight"]:
                if measured:
                    overflow += 1
                    codes["GENERATOR_OVERFLOW"] += 1
                    phase_codes[profile.phase_at(scheduled - warmup) if profile else 0][
                        "GENERATOR_OVERFLOW"
                    ] += 1
            else:
                if measured:
                    lags.append(max(0.0, now - scheduled) * 1000)
                task = asyncio.create_task(call(index, scheduled, measured))
                active.add(task)
                task.add_done_callback(active.discard)
            index += 1
        if index < total:
            await asyncio.sleep(max(0.0, min(DISPATCH_TICK_S, schedule[index] - now)))
    if active:
        await asyncio.gather(*active)
    for channel in channels:
        await channel.close()
    return {
        "worker": worker,
        "latencies": latencies.tobytes(),
        "lags": lags.tobytes(),
        "codes": dict(codes),
        "instances": dict(instances),
        "per_second": [[s, ok, n] for (s, ok), n in per_second.items()],
        "overflow": overflow,
        "phases": [
            {
                "latencies": phase_latencies[p].tobytes(),
                "codes": dict(phase_codes[p]),
                "instances": dict(phase_instances[p]),
            }
            for p in range(phase_count)
        ],
    }


def _worker(config: dict, queue) -> None:
    try:
        queue.put(asyncio.run(_worker_main(config)))
    except BaseException as error:  # report, never hang the parent
        queue.put({"worker": config["worker"], "error": repr(error)})


def step_interval(args) -> float:
    """Fixed spacing between step starts, so several load machines stay in step."""
    return args.warmup + args.duration + args.timeout + 20


def run_step(args, load, step: int, run_id: str) -> tuple[dict, list[dict]]:
    """One measured step: a constant rate, or a load_profile.Profile (changing load)."""
    profile = load if hasattr(load, "phases") else None
    rate = profile.mean_rate if profile else load
    duration = profile.duration if profile else args.duration
    context = mp.get_context("spawn")
    queue = context.Queue()
    # Spawned interpreters need time to import grpc; start every worker on one clock.
    start_delay, late = 3.0, False
    if args.start_at:
        start_delay = args.start_at + step * step_interval(args) - time.time()
        late = start_delay < 2.0
        start_delay = max(start_delay, 2.0)
    base = {
        "targets": args.targets,
        "workers": args.workers,
        "channels": args.channels,
        "rate": rate,
        "profile": profile.spec if profile else None,
        "warmup": args.warmup,
        "duration": duration,
        "timeout": args.timeout,
        "ready_timeout": args.ready_timeout,
        "max_inflight": args.max_inflight,
        "entry": args.entry,
        "workload": args.workload,
        "run_id": run_id,
        "step": step,
        "start_delay": start_delay,
    }
    processes = [
        context.Process(target=_worker, args=({**base, "worker": w}, queue), daemon=True)
        for w in range(args.workers)
    ]
    for process in processes:
        process.start()
    deadline = (
        time.monotonic() + start_delay + args.warmup + duration + args.timeout + args.ready_timeout + 60
    )
    results = []
    while len(results) < len(processes) and time.monotonic() < deadline:
        try:
            results.append(queue.get(timeout=1))
        except Exception:  # queue.Empty
            if not any(p.is_alive() for p in processes) and queue.empty():
                break
    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
    if late:
        results.append({"worker": -1, "error": "started late for --start-at; machines were not in step"})
    return summarize_step(rate, duration, args.workers, results, profile), results


def summarize_step(rate, duration, workers, results, profile=None) -> dict:
    summary = summarize(rate, duration, workers, results)
    if profile is not None:
        summary["profile"] = {"name": profile.name, "spec": profile.spec}
        summary["phases"] = phase_results(profile, workers, results)
    return summary


def phase_results(profile, workers: int, results: list[dict]) -> list[dict]:
    """Per-phase verdicts. Settling here counts errors only (latency is not kept per second)."""
    from load_profile import SETTLE_WINDOW_S

    errors = [r for r in results if "error" in r]
    good = [r for r in results if "error" not in r]
    failed_per_second, offered_per_second = Counter(), Counter()
    for r in good:
        for second, ok, n in r["per_second"]:
            offered_per_second[second] += n
            if not ok:
                failed_per_second[second] += n
    phases = []
    for index, (phase, (start, end)) in enumerate(zip(profile.phases, profile.bounds(), strict=True)):
        records = [
            {
                "latencies": r["phases"][index]["latencies"],
                "lags": b"",
                "codes": r["phases"][index]["codes"],
                "instances": r["phases"][index]["instances"],
                "per_second": [],
            }
            for r in good
        ]
        summary = summarize(phase.mean_rate, phase.duration, workers, records + errors)
        settle = None
        if index:
            second = int(start)
            while second + SETTLE_WINDOW_S <= end:
                window = range(second, second + SETTLE_WINDOW_S)
                if all(offered_per_second[s] and not failed_per_second[s] for s in window):
                    settle = second - start
                    break
                second += 1
        phases.append(
            {
                "phase": index + 1,
                "label": phase.label(),
                "rate": phase.mean_rate,
                "goodput_rps": summary["goodput_rps"],
                "error_percent": summary["error_percent"],
                "latency_ms": summary["latency_ms"],
                "passed": summary["passed"],
                "settle_s": settle,
            }
        )
    return phases


def _raw_name(step: int, phase: int | None, kind: str) -> str:
    return f"step-{step}.{kind}.f32" if phase is None else f"step-{step}.phase-{phase}.{kind}.f32"


def save_raw(
    directory: Path, step: int, rate: float, workers: int, results: list[dict], profile=None
) -> None:
    """Raw aggregates of one step, so runs from several load machines can be merged exactly."""
    latencies, lags = array("f"), array("f")
    phase_count = len(profile.phases) if profile else 0
    per_phase = [array("f") for _ in range(phase_count)]
    rest = []
    for r in results:
        if "error" in r:
            rest.append({"error": r["error"]})
            continue
        latencies.frombytes(r["latencies"])
        lags.frombytes(r["lags"])
        for index in range(phase_count):
            per_phase[index].frombytes(r["phases"][index]["latencies"])
        record = {k: v for k, v in r.items() if k not in ("latencies", "lags", "phases")}
        record["phases"] = [{k: v for k, v in p.items() if k != "latencies"} for p in r.get("phases", [])]
        rest.append(record)
    (directory / _raw_name(step, None, "latencies")).write_bytes(latencies.tobytes())
    (directory / _raw_name(step, None, "lags")).write_bytes(lags.tobytes())
    for index, values in enumerate(per_phase):
        (directory / _raw_name(step, index, "latencies")).write_bytes(values.tobytes())
    (directory / f"step-{step}.json").write_text(
        json.dumps(
            {"rate": rate, "workers": workers, "profile": profile.spec if profile else None, "results": rest}
        ),
        encoding="utf-8",
    )


def load_raw(directory: Path, step: int):
    """(rate, workers, results, profile spec) of one saved step."""
    meta = json.loads((directory / f"step-{step}.json").read_text(encoding="utf-8"))
    results = [dict(r) for r in meta["results"]]
    good = [r for r in results if "error" not in r]
    for r in good:
        r["latencies"] = r["lags"] = b""
        r["phases"] = [dict(p, latencies=b"") for p in r.get("phases", [])]
    if good:
        # All latencies of the step travel with the first good record; the rest carry counts only.
        good[0]["latencies"] = (directory / _raw_name(step, None, "latencies")).read_bytes()
        good[0]["lags"] = (directory / _raw_name(step, None, "lags")).read_bytes()
        if meta.get("profile"):
            for index, phase in enumerate(good[0]["phases"]):
                phase["latencies"] = (directory / _raw_name(step, index, "latencies")).read_bytes()
    return meta["rate"], meta["workers"], results, meta.get("profile")


def summarize(rate: float, duration: float, workers: int, results: list[dict]) -> dict:
    errors = [r["error"] for r in results if "error" in r]
    good = [r for r in results if "error" not in r]
    latencies, lags = array("f"), array("f")
    codes, instances, per_second = Counter(), Counter(), Counter()
    for r in good:
        latencies.frombytes(r["latencies"])
        lags.frombytes(r["lags"])
        codes.update(r["codes"])
        instances.update(r["instances"])
        for second, ok, n in r["per_second"]:
            per_second[(second, ok)] += n
    ordered, lag_ordered = sorted(latencies), sorted(lags)
    scheduled = sum(codes.values())
    attempted = scheduled - codes.get("GENERATOR_OVERFLOW", 0)
    ok = codes.get("OK", 0)
    lag_p99 = percentile(lag_ordered, 99)
    complete = not errors and len(good) == workers
    valid = complete and not codes.get("GENERATOR_OVERFLOW") and (lag_p99 or 0) <= MAX_SCHEDULE_LAG_P99_MS
    error_fraction = 1 - ok / attempted if attempted else 1.0
    goodput = ok / duration
    p99 = percentile(ordered, 99)
    passed = (
        valid
        and error_fraction <= MAX_ERROR_FRACTION
        and goodput >= MIN_GOODPUT_FRACTION * rate
        and p99 is not None
        and p99 < MAX_P99_MS
    )
    by_service: dict[str, dict[str, int]] = {}
    for name, count in instances.items():
        by_service.setdefault(name.split("-", 1)[0], {})[name] = count
    return {
        "rate": rate,
        "valid_load": valid,
        "passed": passed,
        "worker_errors": errors,
        "scheduled": scheduled,
        "attempted": attempted,
        "successful": ok,
        "goodput_rps": goodput,
        "error_percent": error_fraction * 100,
        "codes": dict(codes),
        "latency_ms": {f"p{p}": percentile(ordered, p) for p in (50, 95, 99)},
        "schedule_lag_ms_p99": lag_p99,
        "instances": by_service,
        "per_second": sorted([s, int(o), n] for (s, o), n in per_second.items()),
    }


def markdown(report: dict) -> str:
    lines = [
        "# Kết quả quét năng lực",
        "",
        f"Đích: {', '.join(report['targets'])}. {report['workers']} tiến trình × {report['channels']} "
        f"kết nối, mỗi bước khởi động {report['warmup']:g} s rồi đo {report['duration']:g} s.",
        "",
        f"Một bước **đạt** khi lỗi ≤ {MAX_ERROR_FRACTION:.0%}, goodput ≥ {MIN_GOODPUT_FRACTION:.0%} tải đặt "
        f"và P99 < {MAX_P99_MS} ms. Bước chỉ **hợp lệ** khi bộ phát tải giữ lịch "
        f"(không tràn, P99 trễ lịch ≤ {MAX_SCHEDULE_LAG_P99_MS} ms); bước không hợp lệ phải chạy lại "
        "với nhiều tiến trình hơn hoặc máy phát tải mạnh hơn.",
        "",
        "| RPS đặt | RPM | Goodput (RPS) | Lỗi (%) | P50 (ms) | P95 (ms) | P99 (ms) | Trễ lịch P99 (ms) "
        "| Hợp lệ | Đạt |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]

    def number(value, digits=1):
        return "—" if value is None else f"{value:.{digits}f}"

    for step in report["steps"]:
        latency = step["latency_ms"]
        lines.append(
            f"| {step['rate']:g} | {step['rate'] * 60:,.0f} | {step['goodput_rps']:.0f} "
            f"| {step['error_percent']:.2f} | {number(latency['p50'])} | {number(latency['p95'])} "
            f"| {number(latency['p99'])} | {number(step['schedule_lag_ms_p99'])} "
            f"| {'có' if step['valid_load'] else 'KHÔNG'} | {'đạt' if step['passed'] else 'KHÔNG ĐẠT'} |"
        )
    passed = [s["rate"] for s in report["steps"] if s["passed"]]
    lines += [
        "",
        f"Mức cao nhất đạt: **{max(passed):g} RPS** ({max(passed) * 60:,.0f} RPM)." if passed else "",
    ]
    lines += ["", "## Phân bố theo instance (bước cuối)", ""]
    last = report["steps"][-1] if report["steps"] else None
    if last:
        for service, counts in sorted(last["instances"].items()):
            total = sum(counts.values()) or 1
            shares = ", ".join(f"{name} {count / total:.0%}" for name, count in sorted(counts.items()))
            lines.append(f"- `{service}`: {shares}")
        bad = {code: n for code, n in last["codes"].items() if code != "OK"}
        if bad:
            lines += ["", f"Mã lỗi ở bước cuối: {', '.join(f'`{c}` {n}' for c, n in sorted(bad.items()))}."]
        if last["worker_errors"]:
            lines += ["", "Lỗi tiến trình phát tải: " + "; ".join(last["worker_errors"])]
    for step in report["steps"]:
        if "phases" not in step:
            continue
        lines += [
            "",
            f"## Theo đoạn: hồ sơ `{step['profile']['name']}` ({step['profile']['spec']})",
            "",
            "**Ổn định sau** là số giây từ đầu đoạn đến cửa sổ 5 s đầu tiên không có lỗi; “—” là không "
            "ổn định trong đoạn. Ở đây chỉ xét lỗi (độ trễ không lưu theo giây).",
            "",
            "| Đoạn | Tải | Goodput (RPS) | Lỗi (%) | P50 (ms) | P95 (ms) | P99 (ms) | Ổn định sau (s) | Đạt |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for phase in step["phases"]:
            latency = phase["latency_ms"]
            settle = "" if phase["phase"] == 1 else number(phase["settle_s"], 0)
            lines.append(
                f"| {phase['phase']} | {phase['label']} | {phase['goodput_rps']:.0f} | {phase['error_percent']:.2f} "
                f"| {number(latency['p50'])} | {number(latency['p95'])} | {number(latency['p99'])} | {settle} "
                f"| {'đạt' if phase['passed'] else 'KHÔNG ĐẠT'} |"
            )
    return "\n".join(lines) + "\n"


def merge(inputs: list[Path], output: Path) -> dict:
    """Combine runs that several load machines made at the same time (--start-at)."""
    reports = [json.loads((d / "report.json").read_text(encoding="utf-8")) for d in inputs]
    first = reports[0]
    if any(r["duration"] != first["duration"] or r["warmup"] != first["warmup"] for r in reports):
        raise ValueError("inputs must use the same --duration and --warmup")
    steps = min(len(r["steps"]) for r in reports)
    merged = {
        **first,
        "host": ", ".join(r["host"] for r in reports),
        "targets": sorted({t for r in reports for t in r["targets"]}),
        "workers": sum(r["workers"] for r in reports),
        "merged_from": [str(d) for d in inputs],
        "steps": [],
    }
    from load_profile import Phase, Profile
    from load_profile import parse as parse_profile

    for step in range(steps):
        rate, workers, results, specs = 0.0, 0, [], []
        for directory in inputs:
            part_rate, part_workers, part, spec = load_raw(directory, step)
            rate, workers = rate + part_rate, workers + part_workers
            results += part
            specs.append(spec)
        profile = None
        if any(specs):
            if not all(specs):
                raise ValueError(f"step {step + 1}: some inputs used a profile and some did not")
            parts = [parse_profile(spec) for spec in specs]
            if len({tuple(p.duration for p in part.phases) for part in parts}) != 1:
                raise ValueError("profiles to merge must have the same phases (durations)")
            # Each machine sent a share of every phase; the cluster saw their sum.
            phases = tuple(
                Phase(
                    sum(p.phases[i].start_rate for p in parts),
                    sum(p.phases[i].end_rate for p in parts),
                    d.duration,
                )
                for i, d in enumerate(parts[0].phases)
            )
            profile = Profile(parts[0].name, ",".join(s for s in dict.fromkeys(specs)), phases)
        duration = profile.duration if profile else first["duration"]
        merged["steps"].append(summarize_step(rate, duration, workers, results, profile))
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(markdown(merged), encoding="utf-8")
    return merged


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "merge":
        parser = argparse.ArgumentParser(prog="scale_probe.py merge", description=merge.__doc__)
        parser.add_argument("--inputs", nargs="+", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        merge(args.inputs, args.output)
        print((args.output / "report.md").read_text(encoding="utf-8"))
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--targets", nargs="+", required=True, help="Every ingress host:port")
    load = parser.add_mutually_exclusive_group(required=True)
    load.add_argument("--rates", nargs="+", type=float, help="Offered RPS per step (capacity sweep)")
    load.add_argument(
        "--profile",
        help="Changing load as one step: a preset (spike, step-up, step-down, ramp, low; append xN to "
        "scale, e.g. 'spike x40') or phases like 2000:60,8000:10,2000:90",
    )
    parser.add_argument("--duration", type=float, default=60, help="Measured seconds per step")
    parser.add_argument("--warmup", type=float, default=10, help="Unmeasured seconds before each step")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    parser.add_argument("--channels", type=int, default=4, help="Connections per worker process")
    parser.add_argument("--max-inflight", type=int, default=4096, help="Per worker process")
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--ready-timeout", type=float, default=20)
    parser.add_argument("--entry", default="service-a")
    parser.add_argument("--workload", choices=["light", "heavy"], default="light")
    parser.add_argument("--keep-going", action="store_true", help="Run every step even after one fails")
    parser.add_argument(
        "--start-at",
        type=float,
        help="Unix time of step 1 on every load machine (several machines, then `merge`). "
        "Implies --keep-going: each machine sends only part of the load, so only the merge can judge.",
    )
    parser.add_argument("--output", type=Path, required=True, help="Directory for report.json/report.md")
    args = parser.parse_args()
    if args.start_at is not None and args.start_at < time.time() + 5:
        parser.error("--start-at must be at least 5 s in the future, e.g. $(( $(date +%s) + 60 ))")
    if args.profile:
        from load_profile import parse as parse_profile

        try:
            loads = [parse_profile(args.profile)]
        except ValueError as error:
            parser.error(str(error))
    else:
        loads = list(args.rates)
        if min(loads) <= 0:
            parser.error("Rates must be positive")
    if min(args.duration, args.timeout, args.ready_timeout) <= 0 or args.warmup < 0:
        parser.error("Durations and deadlines must be positive")
    if min(args.workers, args.channels, args.max_inflight) < 1:
        parser.error("--workers, --channels and --max-inflight must be at least 1")
    args.output.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "started_unix_s": time.time(),
        "host": platform.node(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "targets": args.targets,
        "workers": args.workers,
        "channels": args.channels,
        "warmup": args.warmup,
        "duration": args.duration,
        "steps": [],
    }
    for step, load in enumerate(loads):
        profile = load if not isinstance(load, float) else None
        what = f"hồ sơ {profile.name} ({profile.duration:g} s)" if profile else f"{load:g} RPS"
        print(f"bước {step + 1}/{len(loads)}: {what} ...", flush=True)
        result, raw = run_step(args, load, step, run_id)
        save_raw(args.output, step, result["rate"], args.workers, raw, profile)
        report["steps"].append(result)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        (args.output / "report.md").write_text(markdown(report), encoding="utf-8")
        print(
            f"  goodput {result['goodput_rps']:.0f} RPS, lỗi {result['error_percent']:.2f}%, "
            f"P99 {result['latency_ms']['p99']}, trễ lịch P99 {result['schedule_lag_ms_p99']}, "
            f"hợp lệ={result['valid_load']}, đạt={result['passed']}",
            flush=True,
        )
        if result["worker_errors"]:
            print("  lỗi tiến trình phát tải: " + "; ".join(result["worker_errors"]), flush=True)
        for phase in result.get("phases", []):
            print(
                f"  đoạn {phase['phase']} ({phase['label']}): goodput {phase['goodput_rps']:.0f}, "
                f"lỗi {phase['error_percent']:.2f}%, P99 {phase['latency_ms']['p99']}, "
                f"ổn định sau {phase['settle_s']}",
                flush=True,
            )
        if args.start_at:
            continue
        if not result["valid_load"]:
            print("  bước không hợp lệ: bộ phát tải không giữ được lịch; dừng.", flush=True)
            break
        if not result["passed"] and not args.keep_going:
            break
    print((args.output / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
