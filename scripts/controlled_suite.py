"""Run counterbalanced static/xDS trials against an already deployed three-VM lab.

Run on the SSH operator machine: supply a Python hook accepting static or xds as argv[1].
The hook must block until BOTH service-a instances are ready. No shell evaluation.
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode-hook", required=True, type=Path)
    parser.add_argument("--rates", nargs="+", type=float, default=[50, 150])
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--workloads", nargs="+", choices=["light", "heavy"], default=["light", "heavy"])
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument(
        "--resume", action="store_true", help="Reuse completed trials after an operator-link failure"
    )
    args = parser.parse_args()
    if args.repetitions < 2 or args.repetitions % 2 or min(args.rates + [args.duration]) <= 0:
        parser.error("Use a positive rate/duration and an even repetition count >=2")
    args.output.mkdir(parents=True, exist_ok=args.resume)
    reports = []
    failure = None
    try:
        for workload in args.workloads:
            for rate in args.rates:
                for repeat in range(args.repetitions):
                    for mode in ["static", "xds"] if repeat % 2 == 0 else ["xds", "static"]:
                        output = args.output / f"{workload}-{rate:g}-{repeat + 1}-{mode}.json"
                        if args.resume and output.exists():
                            report = json.loads(output.read_text(encoding="utf-8"))
                            expected = {
                                "address": args.address,
                                "mode": mode,
                                "workload": workload,
                                "rate": rate,
                                "duration": args.duration,
                            }
                            if any(report["parameters"][k] != v for k, v in expected.items()):
                                raise ValueError(f"Resume parameters differ: {output}")
                            if (
                                not output.with_suffix(".jsonl").exists()
                                or not report["summary"]["valid_load"]
                            ):
                                raise ValueError(f"Cannot resume an incomplete/invalid trial: {output}")
                            reports.append(report)
                            continue
                        subprocess.run([sys.executable, str(args.mode_hook.resolve()), mode], check=True)
                        subprocess.run(
                            [
                                sys.executable,
                                str(Path(__file__).with_name("controlled_probe.py")),
                                "--address",
                                args.address,
                                "--mode",
                                mode,
                                "--workload",
                                workload,
                                "--rate",
                                str(rate),
                                "--duration",
                                str(args.duration),
                                "--output",
                                str(output),
                            ],
                            check=True,
                        )
                        report = json.loads(output.read_text(encoding="utf-8"))
                        reports.append(report)
                        if not report["summary"]["valid_load"]:
                            raise RuntimeError(f"Generator overflow invalidates offered load: {output}")
    except Exception as exc:
        failure = str(exc)
        raise
    finally:
        # Also restore after a failed trial; retain a separate restoration failure.
        restored = (
            subprocess.run([sys.executable, str(args.mode_hook.resolve()), "xds"], check=False).returncode
            == 0
        )
        groups = {}
        for report in reports:
            parameters, metrics = report["parameters"], report["summary"]
            key = f"{parameters['workload']}/{parameters['rate']}/{parameters['mode']}"
            group = groups.setdefault(key, [])
            group.append(metrics)
        aggregate = {}
        for key, samples in groups.items():
            aggregate[key] = {"repetitions": len(samples), "trials": samples}
            for name in ("successful_rps_offered_window", "rpc_error_fraction"):
                values = [s[name] for s in samples if s[name] is not None]
                aggregate[key][name] = {
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                }
        (args.output / "suite.json").write_text(
            json.dumps({"failure": failure, "restored_xds": restored, "groups": aggregate}, indent=2) + "\n",
            encoding="utf-8",
        )
        if not restored:
            raise RuntimeError("Failed to restore xds; inspect mode hook before continuing")


if __name__ == "__main__":
    main()
