"""Run paired, sequential RL-vs-round-robin xDS experiments and summarize raw RPCs."""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, int((len(ordered) - 1) * fraction))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--train-windows", type=int, default=8)
    parser.add_argument("--eval-windows", type=int, default=8)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--seeds", type=int, nargs="+", default=[31, 32])
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.train_windows < 1 or args.eval_windows < 1 or args.requests < 30:
        parser.error("positive window counts and at least 30 requests required")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("experiment_rl_lookaside.py")
    rows = []
    for run_index, seed in enumerate(args.seeds):
        # Alternate order to limit time/order bias; never overlap experiments.
        modes = ("baseline", "rl") if run_index % 2 else ("rl", "baseline")
        for mode in modes:
            target = out / f"{mode}-{seed}.json"
            cmd = [
                sys.executable,
                str(script),
                "--windows",
                str(args.train_windows + args.eval_windows),
                "--train-windows",
                str(args.train_windows),
                "--requests",
                str(args.requests),
                "--seed",
                str(seed),
                "--output",
                str(target),
            ]
            if mode == "baseline":
                cmd.append("--baseline")
            elif args.model:
                cmd.extend(["--model", args.model])
            cmd.extend(["--slow", "two" if run_index % 2 == 0 else "one"])
            subprocess.run(cmd, check=True)
            data = json.loads(target.read_text(encoding="utf-8"))
            windows = [row for row in data["records"] if row["phase"] == "evaluation"]
            if len(windows) != args.eval_windows or any("fallback" in row for row in data["records"]):
                raise RuntimeError(f"incomplete experiment: {target}")
            durations = [value for row in windows for value in row["latency_ms"]]
            if not durations:
                raise RuntimeError(f"no successful requests: {target}")
            offered = sum(row["offered"] for row in windows)
            rows.append(
                {
                    "mode": mode,
                    "seed": seed,
                    "offered": offered,
                    "success": len(durations),
                    "errors": sum(row["errors"] for row in windows),
                    "mean_ms": statistics.mean(durations),
                    "p95_ms": percentile(durations, 0.95),
                    "p99_ms": percentile(durations, 0.99),
                    "weights": [row["weights_sorted_addresses"] for row in windows],
                    "raw": target.name,
                }
            )
    (out / "report.json").write_text(
        json.dumps({"rows": rows, "conditions": vars(args)}, indent=2), encoding="utf-8"
    )
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
