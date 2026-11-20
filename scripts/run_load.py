"""Sweep offered load against the lab for several caller/ingress variants and report saturation.

Each cell is one fixed-rate trace. Variants are brought up one after another and the order is
shuffled per repetition, so slow drift on the host does not favour one variant.

    python scripts/run_load.py --rates 50 100 200 400 --repetitions 3
    python scripts/run_load.py --variants xds static static-lr --scenarios even slow-node3
    python scripts/run_load.py --report results/lab-load/<run>      # rebuild tables only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path
from types import SimpleNamespace

from controlled_probe import Probe, report_environment, summarize
from labenv import VARIANTS, LabEnvironment, make_lab
from load_profile import (
    PRESETS,
    SETTLE_P95_FACTOR,
    SETTLE_P95_SLACK_MS,
    SETTLE_WINDOW_S,
    Profile,
    phase_summary,
    timeseries,
)
from load_profile import parse as parse_profile
from run_faults import wait_ready

ROOT = Path(__file__).resolve().parents[1]
# Pre-registered pass criteria for one (variant, scenario, rate) cell.
MAX_ERROR_FRACTION = 0.01
MIN_GOODPUT_FRACTION = 0.98
MAX_P99_MS = 3000
SCENARIOS = ("even", "slow-node3")


def prepare(lab: LabEnvironment, scenario: str, slow_ms: int) -> None:
    lab.heal()
    if scenario == "slow-node3":
        # b-2 and c-2 answer slowly: the "uneven backends" case.
        lab.degrade("node3", delay_ms=slow_ms)


def parameters(lab: LabEnvironment, output: Path, load: float | Profile, args) -> SimpleNamespace:
    """Probe settings for one cell; `load` is a constant rate or a changing-load profile."""
    profile = load if isinstance(load, Profile) else None
    return SimpleNamespace(
        address=lab.address,
        output=output,
        mode="lab",
        entry="service-a",
        workload=args.workload,
        rate=profile.mean_rate if profile else load,
        duration=profile.duration if profile else args.duration,
        timeout=3,
        ready_timeout=30,
        max_inflight=args.max_inflight,
        warmup=100,
        fault_at=None,
        stable_window=5,
        profile=profile,
    )


def label(load: float | Profile) -> str:
    return f"profile-{load.name}" if isinstance(load, Profile) else f"rps{load:g}"


async def run_cell(lab: LabEnvironment, directory: Path, variant: str, scenario: str, load, rep, args):
    stem = f"{scenario}-{variant}-{label(load)}-r{rep}"
    output = directory / f"{stem}.json"
    await asyncio.to_thread(prepare, lab, scenario, args.slow_ms)
    try:
        await wait_ready(lab.address)
        if args.settle:
            # Weight controllers act on 10 s windows: give them load and time to react, unmeasured.
            # A profile settles at its first phase's rate, so the measured run starts from steady state.
            first = load.phases[0].start_rate or load.mean_rate if isinstance(load, Profile) else load
            settle = parameters(lab, directory / "settle" / f"{stem}.json", first, args)
            settle.duration = args.settle
            await Probe(settle).run()
        await Probe(parameters(lab, output, load, args)).run()
    finally:
        await asyncio.to_thread(lab.heal)
    meta = {"variant": variant, "scenario": scenario, "repetition": rep}
    if isinstance(load, Profile):
        meta.update(rate=load.mean_rate, profile=load.name, profile_spec=load.spec)
    else:
        meta["rate"] = load
    output.with_suffix(".meta.json").write_text(json.dumps(meta), encoding="utf-8")


def analyse(trace: Path) -> dict:
    meta = json.loads(trace.with_suffix(".meta.json").read_text(encoding="utf-8"))
    report = json.loads(trace.read_text(encoding="utf-8"))
    # One trace in memory at a time: a 2 GiB VM cannot hold every row of a long suite.
    with trace.with_suffix(".jsonl").open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    duration = report["parameters"]["duration"]
    summary = summarize(rows, duration)
    served = [i for row in rows if row["code"] == "OK" for i in row["instances"]]
    b_total = sum(name.startswith("b-") for name in served)
    profile = parse_profile(meta["profile_spec"]) if meta.get("profile_spec") else None
    if profile is not None:
        # Changing load: compare with what was scheduled, not with an average rate.
        goodput_fraction = summary["successful"] / max(1, summary["scheduled"])
    else:
        goodput_fraction = summary["successful_rps_offered_window"] / meta["rate"]
    error_fraction = summary["rpc_error_fraction"] or 0
    p99 = summary["success_latency_ms"]["p99"]
    passed = (
        summary["valid_load"]
        and error_fraction <= MAX_ERROR_FRACTION
        and goodput_fraction >= MIN_GOODPUT_FRACTION
        and p99 is not None
        and p99 < MAX_P99_MS
    )
    result = {
        **meta,
        "trace": trace.name,
        "goodput_rps": summary["successful_rps_offered_window"],
        "goodput_fraction": goodput_fraction,
        "error_percent": error_fraction * 100,
        "generator_drops": summary["codes"].get("GENERATOR_OVERFLOW", 0),
        "p50_ms": summary["success_latency_ms"]["p50"],
        "p95_ms": summary["success_latency_ms"]["p95"],
        "p99_ms": p99,
        "p99_all_ms": summary["all_rpc_latency_ms"]["p99"],
        "share_b2_percent": b_total and served.count("b-2") / b_total * 100,
        "valid_load": summary["valid_load"],
        "passed": passed,
        **({"phases": phase_summary(rows, profile)} if profile is not None else {}),
    }
    if profile is not None:
        # Written now, while the rows are loaded, so no row outlives this call.
        series = timeseries(rows, profile.duration)
        trace.with_name(trace.name.removesuffix(".json") + ".timeseries.csv").write_text(
            "second,offered,ok,failed,p95_ms\n"
            + "".join(
                f"{s['second']},{s['offered']},{s['ok']},{s['failed']},{s['p95_ms'] or ''}\n" for s in series
            ),
            encoding="utf-8",
        )
    return result


def profile_section(directory: Path, runs: list[dict]) -> list[str]:
    """Per-phase table of changing-load runs (their per-second CSVs are written by analyse)."""
    groups: dict[tuple, list[dict]] = {}
    for run in runs:
        groups.setdefault((run["profile"], run["scenario"], run["variant"]), []).append(run)

    def spread(values, digits=1):
        values = [v for v in values if v is not None]
        if not values:
            return "—"
        return f"{statistics.median(values):.{digits}f} [{min(values):.{digits}f}–{max(values):.{digits}f}]"

    lines = [
        "## Tải thay đổi theo đoạn",
        "",
        "Ô là median [min–max] giữa các lần lặp. **Ổn định sau** là số giây từ đầu đoạn đến cửa sổ "
        f"{SETTLE_WINDOW_S} s đầu tiên không lỗi và có P95 ≤ {SETTLE_P95_FACTOR:g} × P95 của đoạn 1 "
        f"(tối thiểu +{SETTLE_P95_SLACK_MS} ms); “—” là không ổn định trong đoạn đó. Chuỗi theo giây "
        "để vẽ đồ thị nằm trong `*.timeseries.csv`.",
    ]
    for (profile, scenario, variant), group in sorted(groups.items()):
        lines += [
            "",
            f"### `{profile}` · {scenario} · {variant} ({len(group)} lần, toàn lượt "
            f"{'đạt' if all(r['passed'] for r in group) else 'KHÔNG ĐẠT'})",
            "",
            "| Đoạn | Tải | Goodput (RPS) | Lỗi (%) | P95 (ms) | P99 (ms) | Phần b-2 (%) | Ổn định sau (s) |",
            "|---:|---|---:|---:|---:|---:|---:|---:|",
        ]
        for index, phase in enumerate(group[0]["phases"]):
            cells = [run["phases"][index] for run in group]
            settles = [c["settle_s"] for c in cells]
            settle = (
                ""
                if index == 0
                else "—"
                if all(s is None for s in settles)
                else spread(settles) + (" (có lần không ổn định)" if None in settles else "")
            )
            lines.append(
                f"| {index + 1} | {phase['label']} | {spread([c['goodput_rps'] for c in cells], 0)} "
                f"| {spread([c['error_percent'] for c in cells], 2)} | {spread([c['p95_ms'] for c in cells])} "
                f"| {spread([c['p99_ms'] for c in cells])} | {spread([c['share_b2_percent'] for c in cells], 0)} "
                f"| {settle} |"
            )
    return lines


def report(directory: Path) -> str:
    runs, incomplete = [], []
    for path in sorted(directory.glob("*.meta.json")):
        trace = path.with_name(path.name.removesuffix(".meta.json") + ".json")
        try:
            runs.append(analyse(trace))
        except (KeyError, OSError, ValueError, ZeroDivisionError):
            incomplete.append(trace.name)
    profiled = [run for run in runs if "phases" in run]
    profile_lines = profile_section(directory, profiled) if profiled else []
    cells: dict[tuple, list[dict]] = {}
    for run in runs:
        if "phases" not in run:
            cells.setdefault((run["scenario"], run["variant"], run["rate"]), []).append(run)

    def spread(values, digits=1):
        values = [value for value in values if value is not None]
        if not values:
            return "—"
        return f"{statistics.median(values):.{digits}f} [{min(values):.{digits}f}–{max(values):.{digits}f}]"

    lines = [
        "# Kết quả tải trên lab",
        "",
        "Ô số là median [min–max] giữa các lần lặp. Một ô (phương án, kịch bản, mức tải) **đạt** khi "
        f"mọi lần lặp thỏa: lỗi ≤ {MAX_ERROR_FRACTION:.0%}, goodput ≥ {MIN_GOODPUT_FRACTION:.0%} tải đặt, "
        f"P99 request thành công < {MAX_P99_MS} ms và bộ phát tải không bỏ request.",
        "",
        "| Kịch bản | Phương án | RPS đặt | Lần lặp | Goodput (RPS) | P95 OK (ms) | P99 OK (ms) "
        "| Lỗi (%) | Phần b-2 (%) | Đạt |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    saturation: dict[tuple, float | None] = {}
    for (scenario, variant, rate), group in sorted(cells.items()):
        ok = all(run["passed"] for run in group)
        lines.append(
            f"| {scenario} | {variant} | {rate:g} | {len(group)} | "
            f"{spread([run['goodput_rps'] for run in group])} | {spread([run['p95_ms'] for run in group])} | "
            f"{spread([run['p99_ms'] for run in group])} | "
            f"{spread([run['error_percent'] for run in group], 2)} | "
            f"{spread([run['share_b2_percent'] for run in group], 0)} | {'đạt' if ok else 'KHÔNG ĐẠT'} |"
        )
        key = (scenario, variant)
        saturation.setdefault(key, None)
        if ok:
            saturation[key] = max(saturation[key] or 0, rate)
    lines += [
        "",
        "## Mức tải cao nhất còn đạt",
        "",
        "| Kịch bản | Phương án | RPS cao nhất đạt |",
        "|---|---|---:|",
    ]
    for (scenario, variant), rate in sorted(saturation.items()):
        lines.append(f"| {scenario} | {variant} | {f'{rate:g}' if rate else 'không mức nào'} |")
    lines += [
        "",
        "Nếu mọi mức đều đạt thì chưa tìm thấy điểm bão hòa: tăng `--rates` rồi chạy lại.",
        "“Phần b-2” là tỷ lệ request B do b-2 (node 3) xử lý; Round Robin cho khoảng 33% (ba instance B), "
        "phương án né backend chậm cho số thấp hơn ở kịch bản slow-node3.",
        report_environment(
            directory,
            "Lab chạy trên một máy, bộ phát tải cũng chung máy: so sánh tương đối giữa các phương án, "
            "không dùng làm số tuyệt đối.",
        ),
        "",
    ]
    if profile_lines:
        lines += [*profile_lines, ""]
    if incomplete:
        lines += [f"Lần chạy không hoàn tất, không tính vào bảng: {', '.join(incomplete)}.", ""]
    text = "\n".join(lines)
    (directory / "report.md").write_text(text, encoding="utf-8")
    (directory / "report.json").write_text(json.dumps(runs, indent=2) + "\n", encoding="utf-8")
    return text


async def suite(args, lab: LabEnvironment, directory: Path) -> None:
    for rep in range(1, args.repetitions + 1):
        order = list(args.variants)
        random.Random(f"{args.seed}-{rep}").shuffle(order)
        for variant in order:
            await asyncio.to_thread(lab.up_variant, variant)
            for scenario in args.scenarios:
                for load in args.loads:
                    what = f"hồ sơ {load.name}" if isinstance(load, Profile) else f"{load:g} RPS"
                    print(f"lần {rep}/{args.repetitions} [{variant}] {scenario} {what}", flush=True)
                    await run_cell(lab, directory, variant, scenario, load, rep, args)


def run_parameters(args) -> dict:
    """Command-line settings as JSON: paths as text, profiles as their phase specs."""

    def plain(value):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, Profile):
            return {"name": value.name, "spec": value.spec}
        if isinstance(value, (list, tuple)):
            return [plain(item) for item in value]
        return value

    return {key: plain(value) for key, value in vars(args).items()}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--variants", nargs="+", choices=list(VARIANTS), default=["xds", "static", "static-lr"]
    )
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument("--rates", nargs="+", type=float, default=[50, 100, 200, 400])
    parser.add_argument(
        "--profiles",
        nargs="+",
        help="Changing load instead of --rates: presets "
        + ", ".join(PRESETS)
        + " (append xN to scale), or phases like 150:60,600:10,150:90",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--duration", type=float, default=30, help="Seconds of measured load per cell")
    parser.add_argument(
        "--settle",
        type=float,
        default=0,
        help="Seconds of unmeasured load before each cell, same for every variant (use 90 with xds-rl/xds-beta)",
    )
    parser.add_argument("--workload", choices=["light", "heavy"], default="light")
    parser.add_argument("--slow-ms", type=int, default=50, help="Delay added to node3 in slow-node3")
    parser.add_argument("--max-inflight", type=int, default=512)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--output", type=Path, help="Run directory (default: results/lab-load/<timestamp>)")
    parser.add_argument("--report", type=Path, help="Only rebuild report.md from an existing run directory")
    parser.add_argument("--wsl", help="WSL distribution holding Docker (Windows only)")
    parser.add_argument("--compose", help="Compose binary on the Docker host")
    parser.add_argument("--env", help="Lab description (JSON) for a multi-VM lab; default is this machine")
    args = parser.parse_args()
    if args.report:
        print(report(args.report))
        return
    if args.repetitions < 1 or args.duration < 10 or min(args.rates) <= 0 or args.slow_ms < 1:
        parser.error("repetitions >= 1, duration >= 10, positive rates and slow-ms required")
    try:
        args.loads = [parse_profile(text) for text in args.profiles] if args.profiles else list(args.rates)
    except ValueError as error:
        parser.error(str(error))
    if args.profiles and len({p.name for p in args.loads}) != len(args.loads):
        parser.error("two --profiles have the same name")
    directory = args.output or ROOT / "results/lab-load" / time.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    lab = make_lab(wsl=args.wsl, compose=args.compose, env=args.env)
    (directory / "run.json").write_text(
        json.dumps(
            {
                "started_unix_s": time.time(),
                "provenance": lab.provenance(),
                "parameters": run_parameters(args),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        with lab.keepalive(), lab.lock(f"run_load -> {directory.name}"):
            asyncio.run(suite(args, lab, directory))
    finally:
        if any(directory.glob("*.meta.json")):
            print(report(directory))


if __name__ == "__main__":
    main()
