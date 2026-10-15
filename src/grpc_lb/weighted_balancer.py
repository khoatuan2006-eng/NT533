from __future__ import annotations

import asyncio
import math
from typing import Mapping, Sequence

from grpc_lb.balancer import BaseBalancer


class WeightedBalancer(BaseBalancer):
    """Bộ cân bằng tải theo trọng số (Smooth Weighted Round Robin - SWRR).

    Thuật toán phân phối tải mượt mà giữa các replica dựa trên trọng số được gán
    từ mô hình Q-learning hoặc cấu hình tĩnh. Thuật toán đảm bảo các request được
    đan xen đều đặn, không dồn cục (bursting) vào backend có trọng số lớn.

    Tuân thủ hợp đồng M2:
      - Nhận ánh xạ backend ID -> trọng số không âm, hữu hạn.
      - Tổng trọng số trên các backend đang khỏe mạnh (healthy) phải lớn hơn 0.
      - Tự động bỏ qua các backend đang bị lỗi (unhealthy) hoặc có trọng số bằng 0.
    """

    def __init__(
        self,
        backends: Sequence[str] | None = None,
        initial_weights: Mapping[str, float | int] | None = None,
    ) -> None:
        super().__init__(backends)
        # Trọng số cấu hình danh định của từng backend (mặc định 1.0 nếu chưa gán)
        self._weights: dict[str, float] = {b: 1.0 for b in self._all_backends}
        # Trọng số tích lũy động dùng cho thuật toán Smooth Weighted Round Robin
        self._current_weights: dict[str, float] = {b: 0.0 for b in self._all_backends}
        # Theo dõi số request đang xử lý (in-flight) của từng replica
        self._in_flight: dict[str, int] = {b: 0 for b in self._all_backends}

        if initial_weights:
            self.set_weights(initial_weights)

    @property
    def policy_name(self) -> str:
        return "weighted"

    def add_backend(self, backend_id: str, is_healthy: bool = True, weight: float = 1.0) -> None:
        """Đăng ký backend mới kèm trọng số mặc định."""
        super().add_backend(backend_id, is_healthy)
        if backend_id not in self._weights:
            self._weights[backend_id] = float(weight)
            self._current_weights[backend_id] = 0.0
            self._in_flight[backend_id] = 0

    def remove_backend(self, backend_id: str) -> None:
        """Xóa backend và dọn dẹp các bảng trọng số/bộ đếm."""
        super().remove_backend(backend_id)
        self._weights.pop(backend_id, None)
        self._current_weights.pop(backend_id, None)
        self._in_flight.pop(backend_id, None)

    def set_weights(self, weights: Mapping[str, float | int]) -> None:
        """Cập nhật ánh xạ trọng số cho các backend.

        Args:
            weights: Ánh xạ backend_id -> trọng số.

        Raises:
            ValueError: Nếu có trọng số âm, vô hạn (NaN/Inf) hoặc tổng trọng số
                        của các backend đang khỏe mạnh <= 0.
        """
        parsed_weights: dict[str, float] = {}
        for b_id, w in weights.items():
            val = float(w)
            if not math.isfinite(val):
                raise ValueError(f"Weight for backend {b_id!r} must be finite, got {w}")
            if val < 0.0:
                raise ValueError(f"Weight for backend {b_id!r} must be non-negative, got {w}")
            parsed_weights[b_id] = val

        # Kiểm tra tổng trọng số trên các backend đang khỏe mạnh
        active_backends = self.get_active_backends()
        # Đối với các backend trong active_backends nhưng không có trong weights mới,
        # giữ nguyên trọng số hiện tại (hoặc 1.0 nếu chưa có)
        total_healthy_weight = 0.0
        for b in active_backends:
            w = parsed_weights.get(b, self._weights.get(b, 1.0))
            total_healthy_weight += w

        if active_backends and total_healthy_weight <= 0.0:
            raise ValueError(
                f"Total weight on healthy backends must be positive, got {total_healthy_weight}"
            )

        # Cập nhật trọng số
        for b_id, w in parsed_weights.items():
            if b_id not in self._all_backends:
                self.add_backend(b_id, is_healthy=True, weight=w)
            else:
                self._weights[b_id] = w

    def get_weights(self) -> dict[str, float]:
        """Trả về bản sao ánh xạ trọng số danh định hiện tại."""
        return dict(self._weights)

    def get_in_flight(self) -> dict[str, int]:
        """Trả về số lượng request in-flight hiện tại của từng replica."""
        return dict(self._in_flight)

    async def pick(self) -> str | None:
        """Chọn replica tiếp theo bằng thuật toán SWRR (có AsyncIO Lock)."""
        async with self._lock:
            return self._pick_unlocked()

    def pick_nowait(self) -> str | None:
        """Phiên bản đồng bộ của pick, phục vụ kiểm thử đơn vị hoặc đồng bộ."""
        return self._pick_unlocked()

    def _pick_unlocked(self) -> str | None:
        active = self.get_active_backends()
        # Chỉ xét các backend đang khỏe mạnh và có trọng số > 0
        eligible = [b for b in active if self._weights.get(b, 0.0) > 0.0]
        if not eligible:
            return None

        total_weight = sum(self._weights[b] for b in eligible)
        if total_weight <= 0.0:
            return None

        # Thuật toán Smooth Weighted Round Robin:
        # Bước 1: current_weight[i] += weight[i] cho tất cả replica hợp lệ
        for b in eligible:
            self._current_weights[b] = self._current_weights.get(b, 0.0) + self._weights[b]

        # Bước 2: Chọn replica có current_weight cao nhất
        # Tie-breaker: thứ tự ổn định trong self._all_backends để đảm bảo tính tất định
        best_backend = max(
            eligible,
            key=lambda b: (self._current_weights[b], -self._all_backends.index(b)),
        )

        # Bước 3: Trừ tổng trọng số khỏi replica được chọn
        self._current_weights[best_backend] -= total_weight

        # Bước 4: Tăng bộ đếm in-flight
        self._in_flight[best_backend] = self._in_flight.get(best_backend, 0) + 1
        return best_backend

    async def on_request_end(self, backend_id: str) -> None:
        """Giảm bộ đếm in-flight khi request kết thúc (bảo vệ bằng Lock)."""
        async with self._lock:
            self._release_unlocked(backend_id)

    def release(self, backend_id: str) -> None:
        """Hàm dọn dẹp đồng bộ an toàn khi gọi trong khối finally."""
        self._release_unlocked(backend_id)

    def _release_unlocked(self, backend_id: str) -> None:
        if backend_id in self._in_flight:
            self._in_flight[backend_id] = max(0, self._in_flight[backend_id] - 1)
