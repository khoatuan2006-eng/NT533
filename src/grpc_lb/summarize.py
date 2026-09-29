from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import statistics
from typing import Any

from .telemetry import read_telemetry


def compute_percentiles(
    values: list[float],
    percentiles: list[float] | None = None,
) -> dict[float, float]:
    """Tính toán các phân vị độ trễ (percentiles) dùng phương pháp nội suy tuyến tính.
    
    Tham số:
        values: Danh sách các giá trị độ trễ (mili-giây).
        percentiles: Danh sách các phân vị cần tính (mặc định [50.0, 95.0, 99.0]).
        
    Trả về:
        Dictionary ánh xạ từ phân vị sang giá trị (ví dụ: {50.0: 12.4, 95.0: 45.2, 99.0: 98.1}).
    """
    if percentiles is None:
        percentiles = [50.0, 95.0, 99.0]

    if not values:
        return {p: 0.0 for p in percentiles}

    sorted_vals = sorted(values)
    n = len(sorted_vals)

    if n == 1:
        return {p: round(sorted_vals[0], 3) for p in percentiles}

    results: dict[float, float] = {}
    for p in percentiles:
        if p <= 0.0:
            results[p] = round(sorted_vals[0], 3)
            continue
        if p >= 100.0:
            results[p] = round(sorted_vals[-1], 3)
            continue

        # Chỉ số vị trí theo phân vị: rank trong khoảng [0, n - 1]
        rank = (p / 100.0) * (n - 1)
        lower_idx = int(math.floor(rank))
        upper_idx = int(math.ceil(rank))
        fraction = rank - lower_idx

        # Nội suy tuyến tính giữa 2 giá trị lân cận
        interpolated = sorted_vals[lower_idx] + fraction * (
            sorted_vals[upper_idx] - sorted_vals[lower_idx]
        )
        results[p] = round(interpolated, 3)

    return results


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Tóm tắt định lượng kết quả của một đợt chạy (1 run)."""

    run_id: str
    policy: str
    total_requests: int
    ok_count: int
    timeout_count: int
    error_count: int
    no_backend_count: int
    ok_rate_pct: float
    timeout_rate_pct: float
    error_rate_pct: float
    duration_s: float
    throughput_rps: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    mean_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    backend_distribution: dict[str, int] = field(default_factory=dict)
    backend_distribution_pct: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AggregatedSummary:
    """Tóm tắt tổng hợp qua nhiều lần chạy lặp lại (ví dụ 3 lần theo M1.md)."""

    policy: str
    runs_count: int
    mean_throughput_rps: float
    std_throughput_rps: float
    mean_p50_latency_ms: float
    mean_p95_latency_ms: float
    mean_p99_latency_ms: float
    mean_ok_rate_pct: float
    mean_timeout_rate_pct: float
    mean_error_rate_pct: float
    aggregated_backend_distribution: dict[str, int] = field(default_factory=dict)


def summarize_records(
    records: list[dict[str, Any]],
    run_id: str | None = None,
    policy: str | None = None,
) -> RunSummary:
    """Phân tích danh sách các bản ghi JSONL và tính toán đầy đủ các chỉ số thống kê."""
    total_requests = len(records)
    if total_requests == 0:
        return RunSummary(
            run_id=run_id or "unknown",
            policy=policy or "unknown",
            total_requests=0,
            ok_count=0,
            timeout_count=0,
            error_count=0,
            no_backend_count=0,
            ok_rate_pct=0.0,
            timeout_rate_pct=0.0,
            error_rate_pct=0.0,
            duration_s=0.0,
            throughput_rps=0.0,
            p50_latency_ms=0.0,
            p95_latency_ms=0.0,
            p99_latency_ms=0.0,
            mean_latency_ms=0.0,
            min_latency_ms=0.0,
            max_latency_ms=0.0,
            backend_distribution={},
            backend_distribution_pct={},
        )

    # Trích xuất metadata run_id và policy nếu chưa được truyền vào
    first_record = records[0]
    effective_run_id = run_id or first_record.get("run_id", "unknown")
    effective_policy = policy or first_record.get("policy", "unknown")

    ok_count = 0
    timeout_count = 0
    error_count = 0
    no_backend_count = 0

    ok_latencies: list[float] = []
    backend_counts: dict[str, int] = {}
    timestamps: list[int] = []

    for r in records:
        status = r.get("status")
        backend = r.get("backend_id") or "none"
        backend_counts[backend] = backend_counts.get(backend, 0) + 1

        t_ns = r.get("started_at_unix_ns")
        if t_ns is not None:
            timestamps.append(t_ns)

        if status == "ok":
            ok_count += 1
            lat = r.get("latency_ms")
            if lat is not None:
                ok_latencies.append(float(lat))
        elif status == "timeout":
            timeout_count += 1
        elif status == "no_backend":
            no_backend_count += 1
        else:
            error_count += 1

    # Tính tỷ lệ phần trăm các trạng thái
    ok_rate = (ok_count / total_requests) * 100.0
    timeout_rate = (timeout_count / total_requests) * 100.0
    error_rate = ((error_count + no_backend_count) / total_requests) * 100.0

    # Tính thời gian chạy (duration) và throughput
    if timestamps and len(timestamps) > 1:
        duration_s = (max(timestamps) - min(timestamps)) / 1_000_000_000.0
    else:
        duration_s = 0.0

    throughput_rps = (ok_count / duration_s) if duration_s > 0 else 0.0

    # Tính phân vị và độ trễ trên tập request OK
    pcts = compute_percentiles(ok_latencies, [50.0, 95.0, 99.0])
    mean_lat = round(statistics.mean(ok_latencies), 3) if ok_latencies else 0.0
    min_lat = round(min(ok_latencies), 3) if ok_latencies else 0.0
    max_lat = round(max(ok_latencies), 3) if ok_latencies else 0.0

    # Tính tỷ lệ phân bổ vào từng replica
    backend_pct: dict[str, float] = {}
    for b_id, count in backend_counts.items():
        backend_pct[b_id] = round((count / total_requests) * 100.0, 2)

    return RunSummary(
        run_id=effective_run_id,
        policy=effective_policy,
        total_requests=total_requests,
        ok_count=ok_count,
        timeout_count=timeout_count,
        error_count=error_count,
        no_backend_count=no_backend_count,
        ok_rate_pct=round(ok_rate, 2),
        timeout_rate_pct=round(timeout_rate, 2),
        error_rate_pct=round(error_rate, 2),
        duration_s=round(duration_s, 3),
        throughput_rps=round(throughput_rps, 2),
        p50_latency_ms=pcts[50.0],
        p95_latency_ms=pcts[95.0],
        p99_latency_ms=pcts[99.0],
        mean_latency_ms=mean_lat,
        min_latency_ms=min_lat,
        max_latency_ms=max_lat,
        backend_distribution=backend_counts,
        backend_distribution_pct=backend_pct,
    )


def summarize_file(file_path: str | Path) -> RunSummary:
    """Đọc trực tiếp 1 file JSONL và trả về RunSummary."""
    records = read_telemetry(file_path)
    return summarize_records(records)


def aggregate_runs(summaries: list[RunSummary]) -> AggregatedSummary:
    """Tổng hợp kết quả của nhiều lần chạy (ví dụ: 3 repetitions)."""
    if not summaries:
        raise ValueError("Danh sách summaries không được để trống")

    policy = summaries[0].policy
    n = len(summaries)

    throughputs = [s.throughput_rps for s in summaries]
    p50s = [s.p50_latency_ms for s in summaries]
    p95s = [s.p95_latency_ms for s in summaries]
    p99s = [s.p99_latency_ms for s in summaries]
    ok_rates = [s.ok_rate_pct for s in summaries]
    timeout_rates = [s.timeout_rate_pct for s in summaries]
    error_rates = [s.error_rate_pct for s in summaries]

    mean_throughput = statistics.mean(throughputs)
    std_throughput = statistics.stdev(throughputs) if n > 1 else 0.0

    agg_distribution: dict[str, int] = {}
    for s in summaries:
        for b_id, count in s.backend_distribution.items():
            agg_distribution[b_id] = agg_distribution.get(b_id, 0) + count

    return AggregatedSummary(
        policy=policy,
        runs_count=n,
        mean_throughput_rps=round(mean_throughput, 2),
        std_throughput_rps=round(std_throughput, 2),
        mean_p50_latency_ms=round(statistics.mean(p50s), 3),
        mean_p95_latency_ms=round(statistics.mean(p95s), 3),
        mean_p99_latency_ms=round(statistics.mean(p99s), 3),
        mean_ok_rate_pct=round(statistics.mean(ok_rates), 2),
        mean_timeout_rate_pct=round(statistics.mean(timeout_rates), 2),
        mean_error_rate_pct=round(statistics.mean(error_rates), 2),
        aggregated_backend_distribution=agg_distribution,
    )


def format_summary_table(summary: RunSummary) -> str:
    """Tạo bảng ASCII tóm tắt kết quả thí nghiệm để in ra console hoặc báo cáo."""
    lines = [
        f"============================================================",
        f" RUN SUMMARY: {summary.run_id} (Policy: {summary.policy})",
        f"============================================================",
        f"  Total Requests   : {summary.total_requests}",
        f"  Success (OK)     : {summary.ok_count} ({summary.ok_rate_pct}%)",
        f"  Timeouts         : {summary.timeout_count} ({summary.timeout_rate_pct}%)",
        f"  Errors / No-back : {summary.error_count + summary.no_backend_count} ({summary.error_rate_pct}%)",
        f"  Duration (s)     : {summary.duration_s}s",
        f"  Throughput       : {summary.throughput_rps} req/s",
        f"------------------------------------------------------------",
        f" LATENCY (OK requests):",
        f"  p50 (Median)     : {summary.p50_latency_ms} ms",
        f"  p95              : {summary.p95_latency_ms} ms",
        f"  p99              : {summary.p99_latency_ms} ms",
        f"  Mean (Avg)       : {summary.mean_latency_ms} ms",
        f"  Min / Max        : {summary.min_latency_ms} ms / {summary.max_latency_ms} ms",
        f"------------------------------------------------------------",
        f" BACKEND DISTRIBUTION:",
    ]
    for b_id, count in sorted(summary.backend_distribution.items()):
        pct = summary.backend_distribution_pct.get(b_id, 0.0)
        lines.append(f"  - {b_id:<14}: {count:>5} requests ({pct:>6.2f}%)")
    lines.append(f"============================================================")
    return "\n".join(lines)
