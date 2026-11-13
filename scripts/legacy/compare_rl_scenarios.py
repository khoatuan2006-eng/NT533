"""Compare a frozen xDS RL policy with round robin and a latency rule over repeated seeds.

Each seed is one independent repetition: the slow instance alternates with the seed and the
order of the three policies is shuffled, so neither position nor run order favours a policy.
"""

import argparse
import json
import random
import statistics
import subprocess
import sys
from pathlib import Path

SCENARIOS = ("equal", "unequal", "swap", "saturated")
MODES = ("baseline", "rl", "rule")
LABELS = {"baseline": "Round Robin", "rl": "RL", "rule": "Quy tắc"}


def run_one(args, out: Path, scenario: str, mode: str, seed: int) -> dict:
    target = out / f"{scenario}-{mode}-{seed}.json"
    cmd = [
        sys.executable,
        str(Path(__file__).with_name("experiment_rl_lookaside.py")),
        "--windows",
        str(args.windows),
        "--train-windows",
        "2",
        "--requests",
        str(args.requests),
        "--concurrency",
        str(args.concurrency),
        "--scenario",
        "unequal" if scenario == "saturated" else scenario,
        "--seed",
        str(seed),
        "--slow",
        "one" if seed % 2 else "two",
        "--output",
        str(target),
    ]
    if scenario == "saturated":
        cmd += ["--backend-capacity", "2"]
    if mode == "rl":
        cmd += ["--model", args.model]
    elif mode == "baseline":
        cmd += ["--baseline"]
    else:
        cmd += ["--rule"]
    subprocess.run(cmd, check=True)
    result = json.loads(target.read_text(encoding="utf-8"))
    windows = [row for row in result["records"] if row["phase"] == "evaluation"]
    values = sorted(x for row in windows for x in row["latency_ms"])
    if not values:
        raise ValueError(f"no successes: {target}")
    return {
        "scenario": scenario,
        "mode": mode,
        "seed": seed,
        "windows": len(windows),
        "fallback": any("fallback" in row for row in result["records"]),
        "offered": sum(row["offered"] for row in windows),
        "errors": sum(row["errors"] for row in windows),
        "mean_ms": statistics.mean(values),
        "p95_ms": values[int((len(values) - 1) * 0.95)],
        "p99_ms": values[int((len(values) - 1) * 0.99)],
        "weights": [row["weights_sorted_addresses"] for row in result["records"]],
        "raw": target.name,
    }


def spread(values: list[float]) -> dict:
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def summarize(rows: list[dict]) -> list[dict]:
    """Across-seed spread per policy, plus paired per-seed differences against RL."""
    summary = []
    for scenario in dict.fromkeys(row["scenario"] for row in rows):
        by_mode = {
            mode: {row["seed"]: row for row in rows if row["scenario"] == scenario and row["mode"] == mode}
            for mode in MODES
        }
        for mode, runs in by_mode.items():
            if not runs:
                continue
            entry = {
                "scenario": scenario,
                "mode": mode,
                "seeds": sorted(runs),
                "mean_ms": spread([run["mean_ms"] for run in runs.values()]),
                "p95_ms": spread([run["p95_ms"] for run in runs.values()]),
                "errors": sum(run["errors"] for run in runs.values()),
                "offered": sum(run["offered"] for run in runs.values()),
                "fallback_runs": sum(run["fallback"] for run in runs.values()),
            }
            if mode != "rl":
                # Negative difference: RL was faster than this policy in that seed.
                shared = sorted(set(runs) & set(by_mode["rl"]))
                differences = [by_mode["rl"][seed]["mean_ms"] - runs[seed]["mean_ms"] for seed in shared]
                if differences:
                    entry["rl_minus_this_mean_ms"] = {
                        "median": statistics.median(differences),
                        "min": min(differences),
                        "max": max(differences),
                        "rl_faster_seeds": sum(d < 0 for d in differences),
                        "seeds": len(differences),
                    }
            summary.append(entry)
    return summary


def markdown(summary: list[dict], args) -> str:
    lines = [
        "# Đối chứng RL — nhiều seed",
        "",
        f"Seed: {', '.join(map(str, args.seeds))}. Mỗi lượt {args.windows} cửa sổ (2 cửa sổ đầu là warmup), "
        f"{args.requests} RPC/cửa sổ, đồng thời {args.concurrency}. Ô latency là median [min–max] giữa các seed.",
        "",
        "| Kịch bản | Policy | Trung bình ms | P95 ms | Lỗi/tổng | Lượt fallback | RL − policy (ms) | RL nhanh hơn |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for entry in summary:
        mean, p95 = entry["mean_ms"], entry["p95_ms"]
        paired = entry.get("rl_minus_this_mean_ms")
        lines.append(
            f"| {entry['scenario']} | {LABELS[entry['mode']]} | "
            f"{mean['median']:.2f} [{mean['min']:.2f}–{mean['max']:.2f}] | "
            f"{p95['median']:.1f} [{p95['min']:.1f}–{p95['max']:.1f}] | "
            f"{entry['errors']}/{entry['offered']} | {entry['fallback_runs']} | "
            + (
                f"{paired['median']:+.2f} [{paired['min']:+.2f}–{paired['max']:+.2f}] | "
                f"{paired['rl_faster_seeds']}/{paired['seeds']} |"
                if paired
                else "— | — |"
            )
        )
    lines += [
        "",
        "Cột “RL − policy” là hiệu latency trung bình theo từng seed; giá trị âm nghĩa là RL nhanh hơn.",
        "Chỉ kết luận RL tốt hơn khi nó nhanh hơn ở hầu hết seed và khoảng [min–max] không chứa 0.",
        "Tải đóng trên localhost; kết quả không đại diện cho tải mở hoặc nhiều caller.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--windows", type=int, default=10)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seeds", type=int, nargs="+", default=[41, 42, 43, 44, 45])
    parser.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS))
    args = parser.parse_args()
    if args.windows < 6 or args.requests < 30 or args.concurrency < 1:
        parser.error("windows >= 6, requests >= 30, concurrency >= 1 required")
    if len(set(args.seeds)) != len(args.seeds):
        parser.error("seeds must be distinct")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for seed in args.seeds:
        for scenario in args.scenarios:
            order = list(MODES)
            random.Random(f"{seed}-{scenario}").shuffle(order)
            for mode in order:
                rows.append(run_one(args, out, scenario, mode, seed))
                # Keep partial evidence if a later run fails.
                (out / "report.json").write_text(
                    json.dumps({"parameters": vars(args), "rows": rows}, indent=2), encoding="utf-8"
                )
    summary = summarize(rows)
    (out / "report.json").write_text(
        json.dumps({"parameters": vars(args), "summary": summary, "rows": rows}, indent=2), encoding="utf-8"
    )
    (out / "report.md").write_text(markdown(summary, args), encoding="utf-8")
    print(markdown(summary, args))


if __name__ == "__main__":
    main()
