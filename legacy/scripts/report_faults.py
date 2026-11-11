"""Recompute fault metrics from raw RPC traces; never rewrite original evidence."""

import argparse
import json
from pathlib import Path

from controlled_probe import interruption, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evidence = {}
    lines = [
        "# Kết quả thử lỗi",
        "",
        "Mỗi tình huống dưới đây chạy một lần ở 20 RPS trong 120 giây.",
        "",
        "| Trace | Mode | OK/tổng | Goodput RPS | P95/P99 OK ms | P95/P99 mọi RPC ms | Lỗi toàn bài % | Lỗi trong fault % | Gián đoạn RPC s | OK khi xác nhận fault |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for path in sorted(args.directory.glob("*.events.json")):
        stem = path.name.removesuffix(".events.json")
        report_path = path.with_name(stem + ".json")
        if not report_path.exists():
            raise ValueError(f"Incomplete probe: {stem}")
        events = json.loads(path.read_text())
        report = json.loads(report_path.read_text())
        rows = [json.loads(line) for line in path.with_name(stem + ".jsonl").read_text().splitlines()]
        if len(rows) != report["summary"]["scheduled"]:
            raise ValueError(f"Trace count mismatch: {stem}")
        duration = report["parameters"]["duration"]
        summary = summarize(rows, duration)
        outage = interruption(rows, events["fault_dispatch_s"], events["restore_dispatch_s"])
        phase = [
            r for r in rows if events["fault_dispatch_s"] <= r["scheduled_s"] < events["restore_dispatch_s"]
        ]
        phase_errors = sum(r["code"] != "OK" for r in phase)
        phase_error_percent = phase_errors / len(phase) * 100 if phase else None
        confirmed = [
            r
            for r in rows
            if events["fault_confirmed_s"] <= r.get("started_s", -1) < events["restore_dispatch_s"]
            and r["code"] == "OK"
        ]
        latency = summary["success_latency_ms"]
        all_latency = summary["all_rpc_latency_ms"]

        def show(value):
            return f"{value:.2f}" if value is not None else "—"

        value = outage["rpc_interruption_s"]
        shown = f"{value:.3f}" if value is not None else "chưa hội tụ"
        lines.append(
            f"| {stem} | {events['mode']} | {summary['successful']}/{summary['scheduled']} | "
            f"{summary['successful_rps_elapsed']:.2f} | {show(latency['p95'])}/{show(latency['p99'])} | "
            f"{show(all_latency['p95'])}/{show(all_latency['p99'])} | "
            f"{summary['rpc_error_fraction'] * 100:.3f} | {show(phase_error_percent)} | {shown} | {len(confirmed)} |"
        )
        evidence[stem] = {
            "events": events,
            "summary": summary,
            "interruption": outage,
            "fault_phase": {
                "scheduled": len(phase),
                "errors": phase_errors,
                "error_percent": phase_error_percent,
            },
            "successes_while_fault_confirmed": len(confirmed),
            "instances_while_fault_confirmed": sorted({i for r in confirmed for i in r["instances"]}),
        }
    lines += [
        "",
        "Gián đoạn đo từ RPC đầu lỗi tới đầu cửa sổ 5 giây toàn OK sau lỗi cuối, trước restore.",
        "0 nghĩa là không ghi nhận lỗi ở tần suất lấy mẫu, không có nghĩa failover tức thì.",
        "Mốc SSH/API dispatch và confirmed nằm trong JSON; không coi độ trễ quản trị là độ trễ ứng dụng.",
        "Lỗi trong fault chỉ tính arrival từ dispatch stop tới dispatch restore; lỗi toàn bài dùng toàn bộ 120 giây.",
        "Đây là số đo theo từng tình huống, chưa có nhiều lần lặp để suy ra phân bố RTO.",
        "",
    ]
    args.output.write_text("\n".join(lines), encoding="utf-8")
    args.output.with_suffix(".json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
