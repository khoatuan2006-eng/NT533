from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any

from .client import WorkloadClient
from .telemetry import TelemetryWriter


@dataclass(frozen=True, slots=True)
class WorkloadConfig:
    """Cấu hình tham số phát tải theo chuẩn đồ án (ví dụ configs/low.json hoặc high.json)."""

    name: str
    rps: float
    duration_s: float
    warmup_s: float = 0.0
    max_in_flight: int = 100
    timeout_s: float = 2.0
    repetitions: int = 1
    seed: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkloadConfig:
        return cls(
            name=data.get("name", "workload"),
            rps=float(data.get("rps", 30.0)),
            duration_s=float(data.get("duration_s", 60.0)),
            warmup_s=float(data.get("warmup_s", 0.0)),
            max_in_flight=int(data.get("max_in_flight", 100)),
            timeout_s=float(data.get("timeout_s", 2.0)),
            repetitions=int(data.get("repetitions", 1)),
            seed=data.get("seed"),
        )

    @classmethod
    def from_file(cls, path: str | Path) -> WorkloadConfig:
        config_path = Path(path)
        with config_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass(frozen=True, slots=True)
class LoadGenReport:
    """Báo cáo tổng kết phiên phát tải của bộ sinh tải (Load Generator)."""

    run_id: str
    target_rps: float
    actual_rps: float
    total_scheduled: int
    total_dispatched: int
    total_dropped: int
    total_completed: int
    duration_s: float
    warmup_requests: int = 0


class WorkloadGenerator:
    """Bộ tạo tải bất đồng bộ theo nhịp độ thời gian thực (Real-time Pacing Load Generator).
    
    Các tính năng kỹ thuật cốt lõi:
    1. Target-Time Pacing: Khử trôi thời gian tích lũy, bảo đảm tốc độ phát bám sát RPS cấu hình.
    2. Concurrency Limiting: Kiểm soát max_in_flight, ghi nhận số request bị drop khi client bão hòa.
    3. Warmup Phase: Hỗ trợ phát tải làm nóng kết nối trước đợt đo lường chính thức.
    4. Telemetry Integration: Tự động chuyển kết quả CallResult sang TelemetryWriter để ghi file JSONL.
    """

    def __init__(
        self,
        client: WorkloadClient,
        telemetry: TelemetryWriter | None = None,
    ) -> None:
        self._client = client
        self._telemetry = telemetry

    async def run(
        self,
        config: WorkloadConfig,
        run_id: str,
    ) -> LoadGenReport:
        """Thực thi một đợt phát tải theo cấu hình WorkloadConfig."""
        if config.rps <= 0:
            raise ValueError(f"RPS phải lớn hơn 0, nhận được: {config.rps}")
        if config.duration_s <= 0:
            raise ValueError(f"Duration phải lớn hơn 0, nhận được: {config.duration_s}")

        # -------------------------------------------------------------
        # Giai đoạn 1: Warmup (Làm nóng hệ thống nếu warmup_s > 0)
        # -------------------------------------------------------------
        warmup_completed = 0
        if config.warmup_s > 0:
            warmup_completed = await self._run_phase(
                rps=config.rps,
                duration_s=config.warmup_s,
                max_in_flight=config.max_in_flight,
                timeout_s=config.timeout_s,
                run_id=f"{run_id}-warmup",
                record_telemetry=False,  # Không ghi dữ liệu warmup vào file kết quả chính thức
            )

        # -------------------------------------------------------------
        # Giai đoạn 2: Measurement (Giai đoạn đo lường chính thức)
        # -------------------------------------------------------------
        start_time = time.perf_counter()
        total_scheduled = int(config.rps * config.duration_s)
        interval = 1.0 / config.rps

        total_dispatched = 0
        total_dropped = 0
        total_completed = 0
        current_in_flight = 0

        active_tasks: set[asyncio.Task[None]] = set()
        loop_start = time.perf_counter()

        for seq in range(1, total_scheduled + 1):
            # Tính mốc thời gian tuyệt đối cho request thứ seq
            target_time = loop_start + ((seq - 1) * interval)
            now = time.perf_counter()
            delay = target_time - now

            if delay > 0:
                await asyncio.sleep(delay)

            # Kiểm tra ngưỡng bão hòa đồng thời của Client (Concurrency Saturation)
            if current_in_flight >= config.max_in_flight:
                total_dropped += 1
                continue

            # Cấp phát 1 slot và phát request
            current_in_flight += 1
            total_dispatched += 1

            req_id = f"{run_id}-req-{seq:06d}"
            task = asyncio.create_task(
                self._dispatch_call(
                    run_id=run_id,
                    request_id=req_id,
                    timeout_s=config.timeout_s,
                    record_telemetry=True,
                )
            )
            active_tasks.add(task)

            # Hàm callback dọn dẹp khi task hoàn tất
            def _on_done(t: asyncio.Task[None]) -> None:
                nonlocal current_in_flight, total_completed
                active_tasks.discard(t)
                current_in_flight = max(0, current_in_flight - 1)
                total_completed += 1

            task.add_done_callback(_on_done)

        # Chờ toàn bộ các request in-flight còn lại hoàn tất (Drain Phase)
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        elapsed_s = time.perf_counter() - start_time
        actual_rps = (total_dispatched / elapsed_s) if elapsed_s > 0 else 0.0

        if self._telemetry:
            self._telemetry.flush()

        return LoadGenReport(
            run_id=run_id,
            target_rps=config.rps,
            actual_rps=round(actual_rps, 2),
            total_scheduled=total_scheduled,
            total_dispatched=total_dispatched,
            total_dropped=total_dropped,
            total_completed=total_completed,
            duration_s=round(elapsed_s, 3),
            warmup_requests=warmup_completed,
        )

    async def _run_phase(
        self,
        rps: float,
        duration_s: float,
        max_in_flight: int,
        timeout_s: float,
        run_id: str,
        record_telemetry: bool,
    ) -> int:
        """Hàm nội bộ phát tải trong 1 khoảng thời gian xác định."""
        total_scheduled = int(rps * duration_s)
        interval = 1.0 / rps
        active_tasks: set[asyncio.Task[None]] = set()
        current_in_flight = 0
        completed = 0
        start = time.perf_counter()

        for seq in range(1, total_scheduled + 1):
            target_time = start + ((seq - 1) * interval)
            delay = target_time - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)

            if current_in_flight >= max_in_flight:
                continue

            current_in_flight += 1
            task = asyncio.create_task(
                self._dispatch_call(
                    run_id=run_id,
                    request_id=f"{run_id}-req-{seq:06d}",
                    timeout_s=timeout_s,
                    record_telemetry=record_telemetry,
                )
            )
            active_tasks.add(task)

            def _on_done(t: asyncio.Task[None]) -> None:
                nonlocal current_in_flight, completed
                active_tasks.discard(t)
                current_in_flight = max(0, current_in_flight - 1)
                completed += 1

            task.add_done_callback(_on_done)

        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        return completed

    async def _dispatch_call(
        self,
        run_id: str,
        request_id: str,
        timeout_s: float,
        record_telemetry: bool,
    ) -> None:
        """Bắn 1 RPC qua client và ghi nhận telemetry nếu được yêu cầu."""
        try:
            result = await self._client.execute(
                run_id=run_id,
                request_id=request_id,
                timeout_s=timeout_s,
            )
            if record_telemetry and self._telemetry is not None:
                self._telemetry.record(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Các lỗi mạng hay gRPC đã được WorkloadClient đóng gói thành CallResult,
            # nếu có ngoại lệ bất thường khác phát sinh thì không làm sập vòng lặp phát tải.
            pass
