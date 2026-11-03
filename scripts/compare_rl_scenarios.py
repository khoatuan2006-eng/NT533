"""Compare a frozen xDS RL policy with round robin and a latency rule, sequentially."""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--windows", type=int, default=10)
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if args.windows < 6 or args.requests < 30 or args.concurrency < 1:
        parser.error("windows >= 6, requests >= 30, concurrency >= 1 required")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for scenario in ("equal", "unequal", "swap", "saturated"):
        for mode in ("baseline", "rl", "rule"):
            target = out / f"{scenario}-{mode}.json"
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
                "41",
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
            rows.append(
                {
                    "scenario": scenario,
                    "mode": mode,
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
            )
    (out / "report.json").write_text(
        json.dumps({"parameters": vars(args), "rows": rows}, indent=2), encoding="utf-8"
    )
    print(json.dumps(rows))


if __name__ == "__main__":
    main()
