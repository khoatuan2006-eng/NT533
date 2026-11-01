"""Regenerate comparison tables from immutable per-RPC traces and run metadata."""

import argparse
import json
import statistics
from pathlib import Path

from controlled_probe import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    groups = {}
    for path in sorted(args.directory.glob("*.json")):
        if not path.with_suffix(".jsonl").exists():
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        parameters = report["parameters"]
        rows = [json.loads(line) for line in path.with_suffix(".jsonl").read_text().splitlines()]
        if len(rows) != report["summary"]["scheduled"]:
            raise ValueError(f"Trace count mismatch: {path}")
        key = (parameters["workload"], parameters["rate"], parameters["mode"])
        groups.setdefault(key, []).append(summarize(rows, parameters["duration"]))
    lines = [
        "# Kết quả đối chứng",
        "",
        "Mỗi ô là median giữa các lượt, không gộp các P99 thành P99 chung.",
        "",
        "| Workload | Offered RPS | Mode | Lượt | Goodput thực tế RPS | P95 OK ms | P99 OK ms | Lỗi RPC % | Đạt |",
        "|---|---:|---|---:|---:|---:|---:|---:|---|",
    ]
    evidence = {}
    for (workload, rate, mode), samples in sorted(groups.items()):

        def median(field):
            values = [value for s in samples if (value := field(s)) is not None]
            return statistics.median(values) if values else None

        def show(value):
            return f"{value:.2f}" if value is not None else "—"

        goodput = median(lambda s: s["successful_rps_elapsed"])
        p95 = median(lambda s: s["success_latency_ms"]["p95"])
        p99 = median(lambda s: s["success_latency_ms"]["p99"])
        error_fraction = median(lambda s: s["rpc_error_fraction"])
        errors = error_fraction * 100 if error_fraction is not None else None
        passed = all(
            s["valid_load"]
            and s["rpc_error_fraction"] is not None
            and s["rpc_error_fraction"] <= 0.01
            and s["successful_rps_offered_window"] >= 0.98 * rate
            and s["success_latency_ms"]["p99"] is not None
            and s["success_latency_ms"]["p99"] < 3000
            for s in samples
        )
        lines.append(
            f"| {workload} | {rate:g} | {mode} | {len(samples)} | {show(goodput)} | {show(p95)} | {show(p99)} | {show(errors)} | {'Có' if passed else 'Không'} |"
        )
        evidence[f"{workload}/{rate}/{mode}"] = {"trials": samples, "passed": passed}
    lines += [
        "",
        "Dữ liệu thô và metadata từng lượt ở thư mục `" + str(args.directory) + "`.",
        "",
        "Xem `docs/controlled-experiments.md` về phương pháp, tiêu chí, nhiễu VPN/SSH và giới hạn thống kê.",
        "Không suy ra lookaside nhanh hơn trong mọi trường hợp từ bảng này.",
        "",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
