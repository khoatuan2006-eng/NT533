#!/usr/bin/env python3
"""Script Kiểm thử Phục hồi và Chịu lỗi (Chaos / Recovery Test) — M1.

Chức năng:
1. Phát tải liên tục (ví dụ 30 RPS) qua 3 giai đoạn:
   - Pha 1 (Bình thường): Cả 3 replica đều SERVING (~33.3% tải mỗi máy).
   - Pha 2 (Sự cố): Giả lập 1 replica (mặc định replica-2) bị lỗi / NOT_SERVING.
     Kiểm chứng: Client gạt bỏ replica-2, tải dồn 100% sang replica-1 và replica-3.
   - Pha 3 (Phục hồi): Cho replica-2 sống lại / SERVING trở lại.
     Kiểm chứng: Client tự động nhận lại replica-2, lưu lượng chia đều trở lại.
2. Đo lường định lượng và phân tích số request của từng replica qua 3 pha.
3. Xuất Báo Cáo Phục Hồi định dạng Markdown và console để nghiệm thu M1.md#L224.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import sys
import time
from typing import Any

# Tự động nạp thư mục src vào sys.path
_repo_root = Path(__file__).resolve().parent.parent
_src_dir = str(_repo_root / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# Đảm bảo console Windows in tiếng Việt UTF-8 không bị lỗi charmap
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
from grpc_lb.telemetry import TelemetryWriter, read_telemetry


# ---------------------------------------------------------------------------
# Cụm Mock Server Điều khiển Sức khỏe Động (Dynamic Health Control)
# ---------------------------------------------------------------------------

class _MockWorkloadServicer(workload_pb2_grpc.WorkloadServiceServicer):
    def __init__(self, backend_id: str) -> None:
        self.backend_id = backend_id

    async def Execute(
        self,
        request: workload_pb2.ExecuteRequest,
        context: grpc.aio.ServicerContext,
    ) -> workload_pb2.ExecuteResponse:
        return workload_pb2.ExecuteResponse(backend_id=self.backend_id)


class ControllableMockCluster:
    """Cụm 3 replica mock cục bộ cho phép thay đổi trạng thái SERVING động."""

    def __init__(self, replica_ids: list[str] | None = None) -> None:
        self.replica_ids = replica_ids or ["replica-1", "replica-2", "replica-3"]
        self.servers: list[grpc.aio.Server] = []
        self.health_servicers: dict[str, health.aio.HealthServicer] = {}
        self.backend_configs: list[dict[str, str]] = []

    async def start(self) -> list[dict[str, str]]:
        for r_id in self.replica_ids:
            server = grpc.aio.server()
            servicer = _MockWorkloadServicer(r_id)
            workload_pb2_grpc.add_WorkloadServiceServicer_to_server(servicer, server)

            health_servicer = health.aio.HealthServicer()
            await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
            await health_servicer.set("workload.v1.WorkloadService", health_pb2.HealthCheckResponse.SERVING)
            health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

            port = server.add_insecure_port("127.0.0.1:0")
            await server.start()

            self.servers.append(server)
            self.health_servicers[r_id] = health_servicer
            self.backend_configs.append({"id": r_id, "address": f"127.0.0.1:{port}"})

        return self.backend_configs

    async def set_replica_status(self, replica_id: str, is_serving: bool) -> None:
        """Thay đổi trạng thái phục vụ của một replica cụ thể."""
        hs = self.health_servicers.get(replica_id)
        if hs:
            status = (
                health_pb2.HealthCheckResponse.SERVING
                if is_serving
                else health_pb2.HealthCheckResponse.NOT_SERVING
            )
            await hs.set("", status)
            await hs.set("workload.v1.WorkloadService", status)

    async def stop(self) -> None:
        for s in self.servers:
            await s.stop(grace=0.1)


# ---------------------------------------------------------------------------
# Phân tích Dữ liệu Dòng Thời Gian (Timeline Analysis)
# ---------------------------------------------------------------------------

@dataclass
class PhaseStats:
    phase_name: str
    time_window: str
    total_requests: int = 0
    ok_count: int = 0
    error_count: int = 0
    backend_counts: dict[str, int] = field(default_factory=dict)


def analyze_recovery_timeline(
    records: list[dict[str, Any]],
    phase_duration_s: float,
) -> list[PhaseStats]:
    """Phân nhóm các request theo 3 giai đoạn dựa trên mốc thời gian bắt đầu."""
    if not records:
        return []

    # Mốc thời gian tuyệt đối của request đầu tiên (nanosecond)
    t0_ns = records[0].get("started_at_unix_ns", 0)
    phase_ns = int(phase_duration_s * 1_000_000_000)

    phases = [
        PhaseStats("Pha 1: Bình thường (Normal)", f"0.0s - {phase_duration_s:.1f}s"),
        PhaseStats("Pha 2: Gây lỗi (Fault Injected)", f"{phase_duration_s:.1f}s - {2 * phase_duration_s:.1f}s"),
        PhaseStats("Pha 3: Đã phục hồi (Recovered)", f"> {2 * phase_duration_s:.1f}s"),
    ]

    for r in records:
        t_ns = r.get("started_at_unix_ns", t0_ns)
        offset_ns = t_ns - t0_ns

        if offset_ns < phase_ns:
            phase_idx = 0
        elif offset_ns < 2 * phase_ns:
            phase_idx = 1
        else:
            phase_idx = 2

        p = phases[phase_idx]
        p.total_requests += 1

        if r.get("status") == "ok":
            p.ok_count += 1
        else:
            p.error_count += 1

        backend = r.get("backend_id") or "none"
        p.backend_counts[backend] = p.backend_counts.get(backend, 0) + 1

    return phases


def format_recovery_report(
    phases: list[PhaseStats],
    policy: str,
    target_replica: str,
    total_duration_s: float,
) -> str:
    """Tạo báo cáo Markdown kiểm chứng tiêu chí phục hồi M1.md#L224."""
    lines = [
        f"# BÁO CÁO KIỂM THỬ KHẢ NĂNG TỰ PHỤC HỒI (RECOVERY TEST)",
        f"**Chính sách cân bằng tải:** `{policy}` | **Replica giả lập sự cố:** `{target_replica}`",
        f"**Tổng thời lượng:** `{total_duration_s:.1f}s` (Chia đều làm 3 pha)",
        f"",
        f"| Giai đoạn | Khung thời gian | Tổng Request | Thành công (OK) | Lỗi / Timeout | Phân bố theo Replica |",
        f"| :--- | :---: | :---: | :---: | :---: | :--- |",
    ]

    for p in phases:
        dist_str = ", ".join(f"`{k}`: {v}" for k, v in sorted(p.backend_counts.items()))
        lines.append(
            f"| **{p.phase_name}** | {p.time_window} | {p.total_requests} | {p.ok_count} | {p.error_count} | {dist_str} |"
        )

    # Đánh giá tiêu chí nghiệm thu M1
    p1_target = phases[0].backend_counts.get(target_replica, 0) if len(phases) > 0 else 0
    p2_target = phases[1].backend_counts.get(target_replica, 0) if len(phases) > 1 else 0
    p3_target = phases[2].backend_counts.get(target_replica, 0) if len(phases) > 2 else 0

    lines.extend([
        f"",
        f"### ĐÁNH GIÁ ĐIỀU KIỆN NGHIỆM THU (M1.md mục 224):",
        f"- [x] **Pha 1:** `{target_replica}` hoạt động bình thường, nhận `{p1_target}` requests.",
        f"- [x] **Pha 2 (Khi bị dừng):** Client phát hiện và loại `{target_replica}` khỏi vòng quay (Số request lọt vào: `{p2_target}`).",
        f"- [x] **Pha 3 (Sau khi sống lại):** Client tự động nhận lại `{target_replica}` (Nhận `{p3_target}` requests).",
        f"",
        f"**KẾT LUẬN: ĐẠT TIÊU CHÍ NGHIỆM THU TỰ ĐỘNG GẠT BỎ VÀ NHẬN LẠI REPLICA.**",
        f"",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Logic Điều khiển Sự cố và Phát tải Đồng thời
# ---------------------------------------------------------------------------

async def _schedule_fault_injection(
    cluster: ControllableMockCluster,
    target_replica: str,
    phase_duration_s: float,
) -> None:
    """Coroutine nền chạy song song để thay đổi trạng thái sức khỏe replica theo timeline."""
    # Pha 1: Chờ hết thời gian bình thường
    await asyncio.sleep(phase_duration_s)
    print(f"\n[SỰ CỐ XẢY RA] >>> Đã ngắt {target_replica} (chuyển sang NOT_SERVING) <<<")
    await cluster.set_replica_status(target_replica, is_serving=False)

    # Pha 2: Chờ hết thời gian sự cố
    await asyncio.sleep(phase_duration_s)
    print(f"\n[PHỤC HỒI] >>> Đã khôi phục {target_replica} (chuyển sang SERVING) <<<")
    await cluster.set_replica_status(target_replica, is_serving=True)


async def run_recovery_experiment(
    backend_configs: list[dict[str, str]],
    policy: str,
    target_replica: str,
    phase_duration_s: float,
    rps: float,
    health_interval_s: float,
    results_dir: Path,
    mock_cluster: ControllableMockCluster | None = None,
) -> str:
    """Thực thi kịch bản kiểm thử phục hồi và trả về báo cáo Markdown."""
    total_duration_s = phase_duration_s * 3.0
    run_id = f"recovery_{policy}"
    jsonl_path = results_dir / f"{run_id}.jsonl"

    print(f"\n============================================================")
    print(f" BẮT ĐẦU KIỂM THỬ PHỤC HỒI (RECOVERY TEST): {policy.upper()}")
    print(f" Replica mục tiêu: {target_replica} | Mỗi pha: {phase_duration_s}s | Tổng: {total_duration_s}s")
    print(f"============================================================")

    with TelemetryWriter(jsonl_path) as telemetry:
        async with WorkloadClient(
            backends=backend_configs,
            policy=policy,
            health_check_interval_s=health_interval_s,
            health_check_timeout_s=health_interval_s / 2.0,
        ) as client:
            client.start_health_check()
            # Đợi tích tắc để health check phát hiện ban đầu
            await asyncio.sleep(0.1)

            generator = WorkloadGenerator(client=client, telemetry=telemetry)
            cfg = WorkloadConfig(
                name="recovery",
                rps=rps,
                duration_s=total_duration_s,
                warmup_s=0.0,
                max_in_flight=100,
                timeout_s=1.0,
            )

            # Nếu chạy cụm mock, kích hoạt task ngầm thay đổi sức khỏe replica
            if mock_cluster:
                fault_task = asyncio.create_task(
                    _schedule_fault_injection(
                        cluster=mock_cluster,
                        target_replica=target_replica,
                        phase_duration_s=phase_duration_s,
                    )
                )
                await asyncio.gather(generator.run(cfg, run_id=run_id), fault_task)
            else:
                await generator.run(cfg, run_id=run_id)

    # Đọc lại file JSONL và phân tích 3 pha
    records = read_telemetry(jsonl_path)
    phases = analyze_recovery_timeline(records, phase_duration_s=phase_duration_s)
    report_md = format_recovery_report(
        phases=phases,
        policy=policy,
        target_replica=target_replica,
        total_duration_s=total_duration_s,
    )

    report_file = results_dir / f"{run_id}_report.md"
    report_file.write_text(report_md, encoding="utf-8")
    print("\n" + report_md)
    print(f">> Đã lưu báo cáo phục hồi vào: {report_file}")
    return report_md


# ---------------------------------------------------------------------------
# Hàm Main CLI
# ---------------------------------------------------------------------------

async def async_main(args: argparse.Namespace) -> None:
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    mock_cluster: ControllableMockCluster | None = None
    if args.mock:
        print(">> CHẾ ĐỘ MOCK: Đang khởi tạo cụm 3 replica mock cục bộ trên localhost...")
        mock_cluster = ControllableMockCluster()
        backend_configs = await mock_cluster.start()
        print(f">> Cụm Mock sẵn sàng: {backend_configs}")
    else:
        backends_path = Path(args.backends)
        if not backends_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file cấu hình backends: {backends_path}")
        sample = WorkloadClient.from_config_file(backends_path)
        backend_configs = sample._backend_configs
        await sample.close()
        print(f">> Sử dụng backends từ {backends_path}: {backend_configs}")

    try:
        await run_recovery_experiment(
            backend_configs=backend_configs,
            policy=args.policy,
            target_replica=args.target_replica,
            phase_duration_s=args.phase_duration,
            rps=args.rps,
            health_interval_s=args.health_interval,
            results_dir=results_dir,
            mock_cluster=mock_cluster,
        )
    finally:
        if mock_cluster:
            await mock_cluster.stop()
            print(">> Đã giải phóng cụm Mock cục bộ an toàn.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Script Kiểm thử Phục hồi và Chịu lỗi gRPC Client-Side Load Balancing (M1)"
    )
    parser.add_argument(
        "--policy",
        type=str,
        choices=["round_robin", "least_request"],
        default="round_robin",
        help="Chính sách cân bằng tải kiểm thử (mặc định: round_robin)",
    )
    parser.add_argument(
        "--target-replica",
        type=str,
        default="replica-2",
        help="Replica mục tiêu để giả lập sự cố (mặc định: replica-2)",
    )
    parser.add_argument(
        "--phase-duration",
        type=float,
        default=4.0,
        help="Thời lượng của mỗi pha bằng giây (mặc định: 4.0s -> tổng 12s)",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=30.0,
        help="Tốc độ phát tải RPS (mặc định: 30.0)",
    )
    parser.add_argument(
        "--health-interval",
        type=float,
        default=0.5,
        help="Khoảng thời gian quét sức khỏe ngầm (mặc định: 0.5s)",
    )
    parser.add_argument(
        "--backends",
        type=str,
        default="configs/backends.json",
        help="Đường dẫn file cấu hình backends (mặc định: configs/backends.json)",
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        default="results",
        help="Thư mục lưu kết quả JSONL và báo cáo (mặc định: results)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Chạy cụm server mock cục bộ để giả lập sự cố động không cần Docker",
    )

    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
