from __future__ import annotations

import abc
import asyncio
from typing import Sequence


class BaseBalancer(abc.ABC):
    """Lớp cơ sở trừu tượng (Abstract Base Class) cho bộ cân bằng tải phía Client.
    
    Định nghĩa các hàm và thuộc tính chung mà mọi thuật toán (Round Robin, Least Request,...)
    đều phải tuân theo.
    """

    def __init__(self, backends: Sequence[str] | None = None) -> None:
        # Danh sách chứa toàn bộ ID của các backend theo thứ tự khai báo ban đầu.
        # Ví dụ: ["replica-1", "replica-2", "replica-3"]
        self._all_backends: list[str] = list(backends) if backends else []

        # Tập hợp (set) lưu các backend HIỆN TẠI ĐANG KHỎE MẠNH (Healthy).
        # Mặc định ban đầu coi tất cả backend khai báo là khỏe mạnh.
        self._healthy_backends: set[str] = set(self._all_backends)

        # Khóa bất đồng bộ (AsyncIO Lock) để đảm bảo an toàn luồng (Concurrency Safety).
        # Ngăn chặn xung đột khi hàng trăm coroutine cùng gọi hàm pick() tại một thời điểm.
        self._lock = asyncio.Lock()

    @property
    @abc.abstractmethod
    def policy_name(self) -> str:
        """Tên của chính sách cân bằng tải (ví dụ: 'round_robin', 'least_request')."""
        ...

    def add_backend(self, backend_id: str, is_healthy: bool = True) -> None:
        """Đăng ký thêm một backend mới vào hệ thống quản lý."""
        if backend_id not in self._all_backends:
            self._all_backends.append(backend_id)
        if is_healthy:
            self._healthy_backends.add(backend_id)
        else:
            self._healthy_backends.discard(backend_id)

    def remove_backend(self, backend_id: str) -> None:
        """Xóa hoàn toàn một backend khỏi hệ thống quản lý."""
        if backend_id in self._all_backends:
            self._all_backends.remove(backend_id)
        self._healthy_backends.discard(backend_id)

    def set_backend_health(self, backend_id: str, is_healthy: bool) -> None:
        """Cập nhật trạng thái sức khỏe của một replica cụ thể.
        
        Được gọi bởi bộ giám sát sức khỏe (Health Checker) khi phát hiện replica
        sống lại (True) hoặc bị ngắt kết nối/quá tải (False).
        """
        if backend_id not in self._all_backends:
            self.add_backend(backend_id, is_healthy)
            return

        if is_healthy:
            self._healthy_backends.add(backend_id)
        else:
            self._healthy_backends.discard(backend_id)

    def set_active_backends(self, active_backend_ids: Sequence[str] | set[str]) -> None:
        """Gán đè toàn bộ danh sách các backend đang hoạt động (active)."""
        active_set = set(active_backend_ids)
        for b_id in active_set:
            if b_id not in self._all_backends:
                self._all_backends.append(b_id)
        self._healthy_backends = active_set

    def get_active_backends(self) -> list[str]:
        """Trả về danh sách các backend vừa có tên trong hệ thống, vừa đang khỏe mạnh.
        
        Giữ nguyên thứ tự ban đầu để đảm bảo tính tất định (deterministic order).
        """
        return [b for b in self._all_backends if b in self._healthy_backends]

    @abc.abstractmethod
    async def pick(self) -> str | None:
        """Chọn một replica phù hợp theo thuật toán và cập nhật bộ đếm (nếu có).
        
        Trả về:
            backend_id (str): ID của replica được chọn.
            None: Nếu hiện tại không có bất kỳ replica nào khỏe mạnh.
        """
        ...

    @abc.abstractmethod
    async def on_request_end(self, backend_id: str) -> None:
        """Hàm callback được gọi khi một RPC kết thúc (thành công, lỗi hoặc timeout)."""
        ...

    def release(self, backend_id: str) -> None:
        """Phiên bản đồng bộ của on_request_end để gọi dọn dẹp trong các khối thông thường."""
        pass


class RoundRobinBalancer(BaseBalancer):
    """Thuật toán Cân bằng tải Round Robin (Luân phiên xoay vòng).
    
    Phân phối các request lần lượt đều qua từng replica đang hoạt động bình thường.
    Ví dụ có 3 replica: 1 -> 2 -> 3 -> 1 -> 2 -> 3...
    Nếu replica-2 bị lỗi, danh sách active chỉ còn [1, 3] -> phân phối 1 -> 3 -> 1 -> 3...
    """

    def __init__(self, backends: Sequence[str] | None = None) -> None:
        super().__init__(backends)
        # Lưu lại ID của backend được chọn ở lần gần nhất
        self._last_picked_id: str | None = None

    @property
    def policy_name(self) -> str:
        return "round_robin"

    async def pick(self) -> str | None:
        """Chọn replica tiếp theo theo thứ tự xoay vòng (có bảo vệ bằng Lock)."""
        async with self._lock:
            return self._pick_unlocked()

    def pick_nowait(self) -> str | None:
        """Hàm chọn đồng bộ dành riêng cho kiểm thử unit test."""
        return self._pick_unlocked()

    def _pick_unlocked(self) -> str | None:
        # Lấy danh sách các replica đang thực sự khỏe mạnh
        active = self.get_active_backends()
        if not active:
            return None  # Không có replica nào khả dụng

        # Nếu chỉ có duy nhất 1 replica sống, luôn luôn chọn máy đó
        if len(active) == 1:
            self._last_picked_id = active[0]
            return active[0]

        # Tìm vị trí của replica vừa chọn lần trước trong danh sách active hiện tại
        if self._last_picked_id in active:
            last_idx = active.index(self._last_picked_id)
            # Công thức xoay vòng: cộng 1 và chia lấy dư (%) cho tổng số replica active
            next_idx = (last_idx + 1) % len(active)
            chosen = active[next_idx]
        else:
            # Nếu lần trước chưa chọn ai (lần đầu) hoặc replica cũ vừa bị sập, chọn phần tử đầu tiên
            chosen = active[0]

        self._last_picked_id = chosen
        return chosen

    async def on_request_end(self, backend_id: str) -> None:
        # Round Robin không cần theo dõi số request đang chạy dở nên không cần làm gì ở đây
        pass

    def release(self, backend_id: str) -> None:
        pass


class LeastRequestBalancer(BaseBalancer):
    """Thuật toán Cân bằng tải Least Request (Ưu tiên máy ít việc nhất).
    
    Khái niệm 'in-flight request': Là số lượng RPC mà client đã gửi đi nhưng
    chưa nhận được phản hồi (đang chạy dở hoặc đang đợi trong hàng đợi của replica).
    
    Quy tắc hoạt động:
    1. Kiểm tra số RPC đang chạy dở (in_flight) của từng replica khỏe mạnh.
    2. Chọn replica có số in_flight nhỏ nhất để giao việc.
    3. Xử lý hòa (Tie-break): Nếu có nhiều replica có cùng số in_flight nhỏ nhất
       (ví dụ lúc mới khởi động tất cả đều = 0), thuật toán sẽ xoay vòng Round Robin
       giữa các replica hòa nhau để chia đều tải, không thiên vị máy nào.
    4. Tăng bộ đếm in_flight của máy được chọn lên 1 ngay lập tức.
    5. Khi request hoàn tất, giảm bộ đếm đi 1 (đảm bảo không bao giờ âm).
    """

    def __init__(self, backends: Sequence[str] | None = None) -> None:
        super().__init__(backends)
        # Dictionary lưu số request đang chạy dở của từng backend: {backend_id: số_lượng}
        # Ví dụ: {"replica-1": 2, "replica-2": 0, "replica-3": 1}
        self._in_flight: dict[str, int] = {b: 0 for b in self._all_backends}
        # Lưu ID của replica vừa được chọn lần trước để phục vụ việc xoay vòng khi hòa điểm
        self._last_picked_id: str | None = None

    @property
    def policy_name(self) -> str:
        return "least_request"

    def add_backend(self, backend_id: str, is_healthy: bool = True) -> None:
        super().add_backend(backend_id, is_healthy)
        if backend_id not in self._in_flight:
            self._in_flight[backend_id] = 0

    def remove_backend(self, backend_id: str) -> None:
        super().remove_backend(backend_id)
        self._in_flight.pop(backend_id, None)

    def get_in_flight(self, backend_id: str) -> int:
        """Xem số lượng request đang chạy dở của một replica cụ thể."""
        return self._in_flight.get(backend_id, 0)

    def total_in_flight(self) -> int:
        """Tổng số request đang chạy dở trên toàn bộ hệ thống (dùng để kiểm thử về 0)."""
        return sum(self._in_flight.values())

    async def pick(self) -> str | None:
        """Chọn replica rảnh nhất và tăng bộ đếm in-flight một cách an toàn (dùng Lock)."""
        async with self._lock:
            return self._pick_unlocked()

    def pick_nowait(self) -> str | None:
        """Hàm chọn đồng bộ dành riêng cho kiểm thử unit test."""
        return self._pick_unlocked()

    def _pick_unlocked(self) -> str | None:
        # Lấy danh sách các replica đang thực sự khỏe mạnh
        active = self.get_active_backends()
        if not active:
            return None

        # Đảm bảo tất cả backend active đều đã có khóa trong dictionary _in_flight
        for b_id in active:
            if b_id not in self._in_flight:
                self._in_flight[b_id] = 0

        # Bước 1: Tìm giá trị in_flight nhỏ nhất trong số các replica đang active
        min_requests = min(self._in_flight[b] for b in active)

        # Bước 2: Lọc ra danh sách các replica có cùng giá trị nhỏ nhất này
        tied_candidates = [b for b in active if self._in_flight[b] == min_requests]

        # Bước 3: Đưa ra quyết định chọn
        if len(tied_candidates) == 1:
            # Chỉ có duy nhất 1 replica rảnh nhất -> chọn luôn máy này
            chosen = tied_candidates[0]
        else:
            # Có từ 2 replica trở lên bằng điểm nhau (Tie): Xoay vòng Round Robin giữa các máy hòa
            if self._last_picked_id in self._all_backends:
                # Quét theo thứ tự vòng tròn bắt đầu từ vị trí kế tiếp sau _last_picked_id
                start_pos = (self._all_backends.index(self._last_picked_id) + 1) % len(self._all_backends)
                ordered = (
                    self._all_backends[start_pos:] + self._all_backends[:start_pos]
                )
                chosen = next((b for b in ordered if b in tied_candidates), tied_candidates[0])
            else:
                chosen = tied_candidates[0]

        # Bước 4: Tăng số lượng request in-flight của máy được chọn lên 1
        self._in_flight[chosen] += 1
        self._last_picked_id = chosen
        return chosen

    async def on_request_end(self, backend_id: str) -> None:
        """Giảm bộ đếm in-flight khi request kết thúc (bảo vệ bằng Lock)."""
        async with self._lock:
            self._release_unlocked(backend_id)

    def release(self, backend_id: str) -> None:
        """Hàm dọn dẹp đồng bộ, an toàn khi gọi trong khối finally."""
        self._release_unlocked(backend_id)

    def _release_unlocked(self, backend_id: str) -> None:
        if backend_id in self._in_flight:
            # Giảm 1 đơn vị, dùng hàm max(0, ...) để ngăn chặn triệt để lỗi số âm
            self._in_flight[backend_id] = max(0, self._in_flight[backend_id] - 1)


def create_balancer(policy: str, backends: Sequence[str] | None = None) -> BaseBalancer:
    """Hàm Factory giúp tạo đối tượng balancer dựa vào chuỗi tên cấu hình.
    
    Hỗ trợ:
      - 'round_robin', 'roundrobin', 'rr' -> RoundRobinBalancer
      - 'least_request', 'leastrequest', 'lr' -> LeastRequestBalancer
    """
    normalized = policy.strip().lower()
    if normalized in ("round_robin", "roundrobin", "rr"):
        return RoundRobinBalancer(backends)
    if normalized in ("least_request", "leastrequest", "lr"):
        return LeastRequestBalancer(backends)
    raise ValueError(f"Unknown load balancing policy: {policy!r}")
