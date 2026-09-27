from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import grpc
from grpc.health.v1 import health_pb2, health_pb2_grpc

from .balancer import BaseBalancer, create_balancer
from .contracts import CallResult, CallStatus
from .generated import workload_pb2, workload_pb2_grpc

logger = logging.getLogger(__name__)


class WorkloadClient:
    """Client gRPC điều phối với tính năng Cân bằng tải phía Client và Giám sát sức khỏe ngầm.
    
    Các vai trò chính của lớp này:
    1. Channel Pool: Quản lý và tái sử dụng các kết nối HTTP/2 (Channel) tới các replica server.
    2. Health Checking: Chạy coroutine ngầm định kỳ ping các replica để phát hiện server chết/sống lại.
    3. Điều phối RPC: Nhận request từ bộ tạo tải (loadgen), hỏi balancer xem nên chuyển đi đâu,
       gửi gRPC request kèm deadline, đo thời gian latency và đóng gói kết quả thành CallResult.
    """

    def __init__(
        self,
        backends: Sequence[Mapping[str, str]],
        policy: str = "round_robin",
        balancer: BaseBalancer | None = None,
        health_check_interval_s: float = 1.0,
        health_check_timeout_s: float = 0.5,
        channel_options: Sequence[tuple[str, Any]] | None = None,
    ) -> None:
        """Khởi tạo WorkloadClient.

        Tham số:
            backends: Danh sách từ điển chứa thông tin các replica, mỗi phần tử gồm 'id' và 'address'.
                      Ví dụ: [{"id": "replica-1", "address": "replica-1:50051"}, ...]
            policy: Tên thuật toán cân bằng tải ('round_robin' hoặc 'least_request').
            balancer: Đối tượng balancer tùy chỉnh (nếu truyền vào thì dùng, không thì tự tạo).
            health_check_interval_s: Khoảng thời gian nghỉ giữa các lần quét sức khỏe (mặc định 1 giây).
            health_check_timeout_s: Thời gian chờ tối đa cho 1 lần ping sức khỏe (mặc định 0.5 giây).
            channel_options: Cấu hình nâng cao cho gRPC channel (nếu có).
        """
        if not backends:
            raise ValueError("Danh sách backends không được để trống")

        self._backend_configs = list(backends)
        self._policy_name = policy
        backend_ids = [b["id"] for b in self._backend_configs]

        # Khởi tạo bộ cân bằng tải (Balancer)
        if balancer is not None:
            self._balancer = balancer
        else:
            self._balancer = create_balancer(policy, backend_ids)

        self._health_interval_s = health_check_interval_s
        self._health_timeout_s = health_check_timeout_s

        # Channel Pool: Lưu trữ các kết nối mạng lâu dài (Persistent Connection)
        # self._channels: {backend_id: grpc.aio.Channel}
        self._channels: dict[str, grpc.aio.Channel] = {}

        # Stub Pool: Lưu trữ các đối tượng gọi RPC ứng dụng (WorkloadService)
        # self._stubs: {backend_id: WorkloadServiceStub}
        self._stubs: dict[str, workload_pb2_grpc.WorkloadServiceStub] = {}

        # Health Stub Pool: Lưu trữ các đối tượng gọi RPC kiểm tra sức khỏe
        # self._health_stubs: {backend_id: HealthStub}
        self._health_stubs: dict[str, health_pb2_grpc.HealthStub] = {}

        # Mở sẵn kết nối (Channel) và tạo Stub cho từng replica ngay từ đầu
        for b in self._backend_configs:
            b_id = b["id"]
            addr = b["address"]
            # Tạo kênh kết nối bất đồng bộ grpc.aio.insecure_channel
            ch = grpc.aio.insecure_channel(addr, options=channel_options)
            self._channels[b_id] = ch
            self._stubs[b_id] = workload_pb2_grpc.WorkloadServiceStub(ch)
            self._health_stubs[b_id] = health_pb2_grpc.HealthStub(ch)

        # Biến lưu Task nền kiểm tra sức khỏe
        self._health_task: asyncio.Task | None = None
        # Cờ đánh dấu client đã đóng hay chưa
        self._is_closed = False

    @classmethod
    def from_config_file(
        cls,
        config_path: str | Path,
        policy: str = "round_robin",
        channel_options: Sequence[tuple[str, Any]] | None = None,
    ) -> WorkloadClient:
        """Hàm tiện ích (Factory method) đọc cấu hình từ file JSON (ví dụ: configs/backends.json)."""
        path = Path(config_path)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        backends = data.get("backends", [])
        health_cfg = data.get("health_check", {})
        interval_s = health_cfg.get("interval_s", 1.0)
        timeout_s = health_cfg.get("timeout_s", 0.5)

        return cls(
            backends=backends,
            policy=policy,
            health_check_interval_s=interval_s,
            health_check_timeout_s=timeout_s,
            channel_options=channel_options,
        )

    @property
    def balancer(self) -> BaseBalancer:
        """Trả về bộ cân bằng tải nội bộ đang sử dụng."""
        return self._balancer

    @property
    def policy(self) -> str:
        """Trả về tên chính sách cân bằng tải hiện hành."""
        return self._balancer.policy_name

    def start_health_check(self) -> asyncio.Task:
        """Kích hoạt tác vụ ngầm định kỳ kiểm tra sức khỏe của các replica."""
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_check_loop())
        return self._health_task

    def stop_health_check(self) -> None:
        """Hủy bỏ tác vụ ngầm kiểm tra sức khỏe."""
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()
            self._health_task = None

    async def check_backend_health(self, backend_id: str) -> bool:
        """Gửi 1 thông điệp kiểm tra sức khỏe tới một replica cụ thể.
        
        Sử dụng giao thức chuẩn gRPC Health Checking Protocol:
        - Gửi HealthCheckRequest(service="")
        - Nếu server trả về SERVING: Replica sống khỏe mạnh (True).
        - Nếu server trả về NOT_SERVING hoặc bị lỗi kết nối: Replica gặp sự cố (False).
        """
        health_stub = self._health_stubs.get(backend_id)
        if not health_stub:
            return False

        try:
            req = health_pb2.HealthCheckRequest(service="")
            resp = await health_stub.Check(req, timeout=self._health_timeout_s)
            is_healthy = (resp.status == health_pb2.HealthCheckResponse.SERVING)
        except Exception:
            # Bất kỳ lỗi mạng, ngắt kết nối hay timeout đều coi replica là unhealthy
            is_healthy = False

        # Cập nhật kết quả sức khỏe vào Balancer để tự động nhận/loại bỏ replica này
        self._balancer.set_backend_health(backend_id, is_healthy)
        return is_healthy

    async def check_all_backends(self) -> dict[str, bool]:
        """Quét và kiểm tra sức khỏe toàn bộ các replica cùng lúc (chạy song song bằng asyncio.gather)."""
        tasks = [
            self.check_backend_health(b["id"])
            for b in self._backend_configs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        health_map: dict[str, bool] = {}
        for b, res in zip(self._backend_configs, results):
            b_id = b["id"]
            health_map[b_id] = res is True
        return health_map

    async def _health_check_loop(self) -> None:
        """Vòng lặp vĩnh cửu chạy trong background để định kỳ theo dõi replica."""
        while not self._is_closed:
            try:
                await self.check_all_backends()
            except asyncio.CancelledError:
                # Thoát vòng lặp khi task bị hủy
                break
            except Exception as e:
                logger.warning("Lỗi trong quá trình quét sức khỏe replica: %s", e)

            # Chờ một khoảng thời gian (interval_s) trước lần quét kế tiếp
            try:
                await asyncio.sleep(self._health_interval_s)
            except asyncio.CancelledError:
                break

    async def execute(
        self,
        run_id: str,
        request_id: str,
        timeout_s: float,
    ) -> CallResult:
        """Hàm thực thi một RPC WorkloadService.Execute có cân bằng tải và đo lường.
        
        Đây là hàm giao diện cốt lõi kết nối với bộ phát tải (loadgen) của bạn Đạt.

        Quy trình xử lý:
        1. Ghi lại mốc thời gian bắt đầu (nanosecond).
        2. Nhờ Balancer chọn 1 replica khỏe mạnh (và tăng biến đếm in-flight nếu là Least Request).
        3. Nếu không có máy nào sống -> trả ngay kết quả với trạng thái NO_BACKEND.
        4. Gửi request gRPC Execute tới replica được chọn kèm thời hạn timeout_s.
        5. Đặt trong khối try ... finally: Bắt buộc gọi on_request_end() để giảm biến đếm in-flight.
        6. Bắt và phân loại lỗi: DEADLINE_EXCEEDED -> TIMEOUT, lỗi khác -> ERROR.
        7. Tính độ trễ latency (ms) và đóng gói đối tượng CallResult theo đúng contracts.py.
        """
        # Bước 1: Ghi nhận thời điểm bắt đầu theo chuẩn Unix Nanosecond
        start_ns = time.time_ns()

        # Bước 2: Hỏi balancer chọn replica
        backend_id = await self._balancer.pick()

        # Bước 3: Kiểm tra nếu toàn bộ replica đều chết
        if backend_id is None:
            end_ns = time.time_ns()
            latency_ms = (end_ns - start_ns) / 1_000_000.0
            return CallResult(
                run_id=run_id,
                request_id=request_id,
                policy=self.policy,
                backend_id=None,
                started_at_unix_ns=start_ns,
                latency_ms=latency_ms,
                status=CallStatus.NO_BACKEND,
                error_message="No healthy backend available",
            )

        # Lấy Stub của replica đã chọn và chuẩn bị dữ liệu request
        stub = self._stubs[backend_id]
        req = workload_pb2.ExecuteRequest(run_id=run_id, request_id=request_id)

        call_status = CallStatus.OK
        grpc_code: str | None = None
        error_message: str | None = None

        # Bước 4 & 5: Gửi RPC và BẢO ĐẢM GIẢM BỘ ĐẾM BẰNG FINALLY
        try:
            # Gửi RPC bất đồng bộ có kèm thời hạn deadline
            await stub.Execute(req, timeout=timeout_s)
        except grpc.aio.AioRpcError as rpc_err:
            # Bắt ngoại lệ chuẩn từ thư viện gRPC
            code = rpc_err.code()
            grpc_code = code.name if code else None
            error_message = rpc_err.details() or str(rpc_err)

            # Phân loại mã lỗi: DEADLINE_EXCEEDED tức là quá thời gian chờ -> TIMEOUT
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                call_status = CallStatus.TIMEOUT
            else:
                call_status = CallStatus.ERROR
        except asyncio.TimeoutError:
            # Bắt lỗi quá thời gian chờ từ phía asyncio của Python
            call_status = CallStatus.TIMEOUT
            error_message = f"Hết thời gian chờ phía client sau {timeout_s}s"
        except asyncio.CancelledError:
            # BẮT BUỘC PHẢI PHÁT LẠI (re-raise) ngoại lệ CancelledError theo đặc tả của M1
            # để đảm bảo ứng dụng có thể dừng sạch sẽ khi người dùng ngắt chương trình.
            raise
        except Exception as exc:
            # Bắt mọi ngoại lệ không mong muốn khác
            call_status = CallStatus.ERROR
            error_message = f"{type(exc).__name__}: {exc}"
        finally:
            # KHỐI FINALLY LUÔN LUÔN CHẠY:
            # Đảm bảo hàm on_request_end được gọi đúng 1 lần duy nhất để giảm biến đếm
            # in-flight của LeastRequest, dù RPC thành công, lỗi mạng, timeout hay bị hủy!
            await self._balancer.on_request_end(backend_id)

        # Bước 6: Tính toán tổng thời gian phản hồi (Latency) bằng mili-giây
        end_ns = time.time_ns()
        latency_ms = (end_ns - start_ns) / 1_000_000.0

        # Bước 7: Trả về đối tượng CallResult chuẩn mực
        return CallResult(
            run_id=run_id,
            request_id=request_id,
            policy=self.policy,
            backend_id=backend_id,
            started_at_unix_ns=start_ns,
            latency_ms=latency_ms,
            status=call_status,
            grpc_code=grpc_code,
            error_message=error_message,
        )

    async def close(self) -> None:
        """Đóng toàn bộ client, dừng kiểm tra sức khỏe và giải phóng các Channel."""
        self._is_closed = True
        self.stop_health_check()
        # Đóng tất cả kênh kết nối TCP gRPC
        close_tasks = [ch.close() for ch in self._channels.values()]
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)

    async def __aenter__(self) -> WorkloadClient:
        """Hỗ trợ cú pháp async with WorkloadClient(...) as client:"""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Tự động gọi close() khi thoát khỏi khối async with."""
        await self.close()
