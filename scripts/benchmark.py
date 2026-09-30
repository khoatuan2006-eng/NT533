#!/usr/bin/env python3
"""Script Benchmark tự động chạy ma trận thí nghiệm cân bằng tải (M1).

Chức năng:
1. Đọc cấu hình tải (low.json hoặc high.json) và danh sách backend (backends.json).
2. Tự động chạy ma trận:
   - Thuật toán Round Robin (lặp N lần).
   - Thuật toán Least Request (lặp N lần).
3. Thu thập dữ liệu gốc JSONL vào thư mục results/.
4. Tính toán p50/p95/p99, throughput, tỷ lệ lỗi và phân bố tải theo replica.
5. Xuất bảng so sánh đối chiếu định dạng Markdown và console.
6. Hỗ trợ cờ --mock để chạy thử nghiệm độc lập ngay trên máy local không cần Docker.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time
from typing import Any

# Tự động nạp thư mục src vào sys.path để có thể chạy script trực tiếp từ bất kỳ thư mục nào
_repo_root = Path(__file__).resolve().parent.parent
_src_dir = str(_repo_root / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# Đảm bảo console Windows in được tiếng Việt UTF-8 không bị lỗi charmap
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


import grpc
from grpc.health.v1 import health, health_pb2, health_pb2_grpc

from grpc_lb.client import WorkloadClient
from grpc_lb.generated import workload_pb2, workload_pb2_grpc
from grpc_lb.loadgen import WorkloadConfig, WorkloadGenerator
from grpc_lb.summarize import (
    AggregatedSummary,
    RunSummary,
    aggregate_runs,
    format_summary_table,
    summarize_file,
)
from grpc_lb.telemetry import TelemetryWriter


# ---------------------------------------------------------------------------
# Cụm Mock Server Cục bộ (Hỗ trợ chạy test độc lập không cần Docker)
# ---------------------------------------------------------------------------

class _MockWorkloadServicer(workload_pb2_grpc.WorkloadServiceServicer):
    def __init__(self, backend_id: str, delay_s: float = 0.0) -> None:
        self.backend_id = backend_id
        self.delay_s = delay_s

    async def Execute(
        self,
        request: workload_pb2.ExecuteRequest,
        context: grpc.aio.ServicerContext,
    ) -> workload_pb2.ExecuteResponse:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return workload_pb2.ExecuteResponse(backend_id=self.backend_id)


class LocalMockCluster:
    """Dựng cụm 3 replica gRPC server tại localhost để chạy benchmark nhanh."""

    def __init__(
        self,
        replica_ids: list[str] | None = None,
        slow_replica_id: str | None = None,
        slow_delay_s: float = 0.05,
    ) -> None:
        self.replica_ids = replica_ids or ["replica-1", "replica-2", "replica-3"]
        self.slow_replica_id = slow_replica_id
        self.slow_delay_s = slow_delay_s
        self.servers: list[grpc.aio.Server] = []
        self.backend_configs: list[dict[str, str]] = []

    async def start(self) -> list[dict[str, str]]:
        for r_id in self.replica_ids:
            delay = self.slow_delay_s if (self.slow_replica_id and r_id == self.slow_replica_id) else 0.0
            server = grpc.aio.server()
            servicer = _MockWorkloadServicer(r_id, delay_s=delay)
            workload_pb2_grpc.add_WorkloadServiceServicer_to_server(servicer, server)

            # Cung cấp Health Service báo SERVING
            health_servicer = health.aio.HealthServicer()
            await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
            await health_servicer.set("workload.v1.WorkloadService", health_pb2.HealthCheckResponse.SERVING)
            health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()
            self.servers.append(server)
            self.backend_configs.append({"id": r_id, "address": f"127.0.0.1:{port}"})

        return self.backend_configs

    async def stop(self) -> None:
        for s in self.servers:
            await s.stop(grace=0.1)


# ---------------------------------------------------------------------------
# Logic Điều phối Benchmark
# ---------------------------------------------------------------------------

async def run_single_run(
    backend_configs: list[dict[str, str]],
    policy: str,
    config: WorkloadConfig,
    run_id: str,
    results_dir: Path,
) -> RunSummary:
    """Thực thi 1 lần chạy (repetition) đơn lẻ và lưu telemetry ra JSONL."""
    jsonl_path = results_dir / f"{run_id}.jsonl"
    print(f"  -> Đang chạy {run_id} ({config.rps} RPS, {config.duration_s}s)...")

    with TelemetryWriter(jsonl_path) as telemetry:
        async with WorkloadClient(
            backends=backend_configs,
            policy=policy,
        ) as client:
            client.start_health_check()
            # Đợi một tích tắc để health check hoàn tất phát hiện các replica
            await asyncio.sleep(0.1)

            generator = WorkloadGenerator(client=client, telemetry=telemetry)
            report = await generator.run(config=config, run_id=run_id)

    # Đọc lại file JSONL và tính toán các chỉ số thống kê
    summary = summarize_file(jsonl_path)
    print(
        f"     Hoàn tất: Total={summary.total_requests}, OK={summary.ok_count} "
        f"({summary.ok_rate_pct}%), Throughput={summary.throughput_rps} req/s, "
        f"p50={summary.p50_latency_ms}ms, p95={summary.p95_latency_ms}ms"
    )
    return summary


async def run_policy_benchmark(
    backend_configs: list[dict[str, str]],
    policy: str,
    config: WorkloadConfig,
    repetitions: int,
    results_dir: Path,
) -> tuple[AggregatedSummary, list[RunSummary]]:
    """Chạy lặp lại N lần cho 1 chính sách (policy) và tổng hợp kết quả."""
    print(f"\n============================================================")
    print(f" BẮT ĐẦU CHÍNH SÁCH: {policy.upper()} ({repetitions} LẦN LẶP)")
    print(f"============================================================")

    summaries: list[RunSummary] = []
    for rep in range(1, repetitions + 1):
        run_id = f"{config.name}_{policy}_rep{rep}"
        summary = await run_single_run(
            backend_configs=backend_configs,
            policy=policy,
            config=config,
            run_id=run_id,
            results_dir=results_dir,
        )
        summaries.append(summary)

    agg = aggregate_runs(summaries)
    return agg, summaries


def generate_comparison_table(
    rr_agg: AggregatedSummary,
    lr_agg: AggregatedSummary,
    workload_name: str,
) -> str:
    """Tạo bảng so sánh Markdown trực diện giữa Round Robin và Least Request."""
    lines = [
        f"# BẢNG SO SÁNH HIỆU NĂNG: ROUND ROBIN VS LEAST REQUEST",
        f"**Kịch bản tải:** `{workload_name}` | **Số lần lặp lại:** `{rr_agg.runs_count}`",
        f"",
        f"| Tiêu chí đánh giá | Round Robin | Least Request | Chênh lệch (LR vs RR) |",
        f"| :--- | :---: | :---: | :---: |",
        f"| **Thông lượng (Throughput)** | `{rr_agg.mean_throughput_rps} ± {rr_agg.std_throughput_rps}` req/s | `{lr_agg.mean_throughput_rps} ± {lr_agg.std_throughput_rps}` req/s | {lr_agg.mean_throughput_rps - rr_agg.mean_throughput_rps:+.2f} req/s |",
        f"| **Độ trễ p50 (Median)** | `{rr_agg.mean_p50_latency_ms}` ms | `{lr_agg.mean_p50_latency_ms}` ms | {lr_agg.mean_p50_latency_ms - rr_agg.mean_p50_latency_ms:+.3f} ms |",
        f"| **Độ trễ p95** | `{rr_agg.mean_p95_latency_ms}` ms | `{lr_agg.mean_p95_latency_ms}` ms | {lr_agg.mean_p95_latency_ms - rr_agg.mean_p95_latency_ms:+.3f} ms |",
        f"| **Độ trễ p99 (Đuôi trễ)** | `{rr_agg.mean_p99_latency_ms}` ms | `{lr_agg.mean_p99_latency_ms}` ms | {lr_agg.mean_p99_latency_ms - rr_agg.mean_p99_latency_ms:+.3f} ms |",
        f"| **Tỷ lệ thành công (OK)** | `{rr_agg.mean_ok_rate_pct}%` | `{lr_agg.mean_ok_rate_pct}%` | {lr_agg.mean_ok_rate_pct - rr_agg.mean_ok_rate_pct:+.2f}% |",
        f"| **Tỷ lệ Timeout** | `{rr_agg.mean_timeout_rate_pct}%` | `{lr_agg.mean_timeout_rate_pct}%` | {lr_agg.mean_timeout_rate_pct - rr_agg.mean_timeout_rate_pct:+.2f}% |",
        f"",
        f"### Phân bố Request theo Replica (Tổng hợp qua tất cả các lần chạy):",
        f"",
        f"| Replica | Round Robin (Số request) | Least Request (Số request) |",
        f"| :--- | :---: | :---: |",
    ]

    all_keys = sorted(
        set(rr_agg.aggregated_backend_distribution.keys())
        | set(lr_agg.aggregated_backend_distribution.keys())
    )
    for k in all_keys:
        rr_cnt = rr_agg.aggregated_backend_distribution.get(k, 0)
        lr_cnt = lr_agg.aggregated_backend_distribution.get(k, 0)
        lines.append(f"| `{k}` | {rr_cnt} | {lr_cnt} |")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hàm Main CLI
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    workload_path = Path(args.workload)
    backends_path = Path(args.backends)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if not workload_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file cấu hình workload: {workload_path}")

    # Đọc cấu hình workload
    base_config = WorkloadConfig.from_file(workload_path)
    repetitions = args.repetitions if args.repetitions is not None else base_config.repetitions

    # Ghi đè duration nếu có cờ chỉ định (ví dụ khi test nhanh)
    if args.duration is not None:
        base_config = WorkloadConfig(
            name=base_config.name,
            rps=base_config.rps,
            duration_s=float(args.duration),
            warmup_s=0.0 if args.skip_warmup else base_config.warmup_s,
            max_in_flight=base_config.max_in_flight,
            timeout_s=base_config.timeout_s,
            repetitions=repetitions,
            seed=base_config.seed,
        )

    # Xác định danh sách backends (qua mock hoặc file backends.json)
    mock_cluster: LocalMockCluster | None = None
    if args.mock:
        print(">> CHẾ ĐỘ MOCK: Đang khởi tạo cụm 3 replica mock cục bộ trên localhost...")
        mock_cluster = LocalMockCluster(
            slow_replica_id=args.slow_replica,
            slow_delay_s=args.slow_delay,
        )
        backend_configs = await mock_cluster.start()
        print(f">> Cụm Mock sẵn sàng: {backend_configs}")
    else:
        if not backends_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file cấu hình backends: {backends_path}")
        client_sample = WorkloadClient.from_config_file(backends_path)
        backend_configs = client_sample._backend_configs
        await client_sample.close()
        print(f">> Sử dụng backends từ {backends_path}: {backend_configs}")

    try:
        policies_to_run = (
            ["round_robin", "least_request"]
            if args.policy == "both"
            else [args.policy]
        )

        aggregated_results: dict[str, AggregatedSummary] = {}
        for pol in policies_to_run:
            agg, _ = await run_policy_benchmark(
                backend_configs=backend_configs,
                policy=pol,
                config=base_config,
                repetitions=repetitions,
                results_dir=results_dir,
            )
            aggregated_results[pol] = agg

        # Nếu chạy cả 2 chính sách, xuất bảng so sánh
        if "round_robin" in aggregated_results and "least_request" in aggregated_results:
            table_md = generate_comparison_table(
                rr_agg=aggregated_results["round_robin"],
                lr_agg=aggregated_results["least_request"],
                workload_name=base_config.name,
            )
            print("\n" + table_md)

            # Lưu bảng so sánh vào thư mục results
            report_file = results_dir / f"{base_config.name}_comparison.md"
            report_file.write_text(table_md, encoding="utf-8")
            print(f"\n>> Đã lưu bảng so sánh Markdown vào: {report_file}")

    finally:
        if mock_cluster:
            await mock_cluster.stop()
            print(">> Đã giải phóng cụm Mock cục bộ an toàn.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Script Benchmark cân bằng tải gRPC phía client (M1)"
    )
    parser.add_argument(
        "--workload",
        type=str,
        default="configs/low.json",
        help="Đường dẫn file cấu hình workload (mặc định: configs/low.json)",
    )
    parser.add_argument(
        "--backends",
        type=str,
        default="configs/backends.json",
        help="Đường dẫn file cấu hình backends (mặc định: configs/backends.json)",
    )
    parser.add_argument(
        "--policy",
        type=str,
        choices=["round_robin", "least_request", "both"],
        default="both",
        help="Chính sách cần đánh giá: round_robin, least_request hoặc both (mặc định: both)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=None,
        help="Số lần lặp lại (ghi đè giá trị trong file cấu hình)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Thời lượng phát tải mỗi lần chạy bằng giây (dùng để test nhanh)",
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Bỏ qua giai đoạn warmup để test nhanh",
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        default="results",
        help="Thư mục lưu trữ kết quả JSONL và báo cáo (mặc định: results)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Chạy cụm server mock cục bộ (không cần Docker)",
    )
    parser.add_argument(
        "--slow-replica",
        type=str,
        default=None,
        help="ID của replica giả lập chạy chậm (ví dụ: replica-2)",
    )
    parser.add_argument(
        "--slow-delay",
        type=float,
        default=0.05,
        help="Thời gian trễ (giây) của replica chạy chậm (mặc định: 0.05s)",
    )

    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
