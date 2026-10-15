from __future__ import annotations

import math
import pytest

from grpc_lb.balancer import create_balancer
from grpc_lb.weighted_balancer import WeightedBalancer


def test_weighted_balancer_basic_proportions():
    """Kiểm tra phân phối tỉ lệ mượt mà (SWRR) với tỉ lệ 5:1:1."""
    balancer = WeightedBalancer(
        backends=["replica-1", "replica-2", "replica-3"],
        initial_weights={"replica-1": 5, "replica-2": 1, "replica-3": 1},
    )

    # Thuật toán SWRR với 7 request sẽ phân phối: 5 cho replica-1, 1 cho replica-2, 1 cho replica-3
    picks = [balancer.pick_nowait() for _ in range(7)]
    counts = {b: picks.count(b) for b in ["replica-1", "replica-2", "replica-3"]}

    assert counts["replica-1"] == 5
    assert counts["replica-2"] == 1
    assert counts["replica-3"] == 1

    # Kiểm tra tính mượt mà: replica-1 không bị dồn cục liên tiếp cả 5 lần
    # Chuỗi thông thường của SWRR 5:1:1 là: 1, 1, 2, 1, 3, 1, 1 (hoặc tương đương)
    assert picks[0] == "replica-1"
    assert picks[1] == "replica-1"
    assert picks[2] in ("replica-2", "replica-3")


def test_weighted_balancer_dynamic_set_weights():
    """Kiểm tra thay đổi trọng số động trong lúc đang chạy."""
    balancer = WeightedBalancer(backends=["A", "B"])
    balancer.set_weights({"A": 1, "B": 1})

    picks_equal = [balancer.pick_nowait() for _ in range(10)]
    assert picks_equal.count("A") == 5
    assert picks_equal.count("B") == 5

    # Đổi sang ưu tiên B: A=1, B=3
    balancer.set_weights({"A": 1, "B": 3})
    picks_b = [balancer.pick_nowait() for _ in range(8)]
    assert picks_b.count("A") == 2
    assert picks_b.count("B") == 6


def test_weighted_balancer_validation():
    """Kiểm tra ràng buộc hợp lệ của trọng số."""
    balancer = WeightedBalancer(backends=["A", "B"])

    # Trọng số âm -> ValueError
    with pytest.raises(ValueError, match="non-negative"):
        balancer.set_weights({"A": -1, "B": 2})

    # Trọng số vô hạn -> ValueError
    with pytest.raises(ValueError, match="finite"):
        balancer.set_weights({"A": math.inf, "B": 2})

    with pytest.raises(ValueError, match="finite"):
        balancer.set_weights({"A": float("nan"), "B": 2})

    # Tổng trọng số các backend khỏe <= 0 -> ValueError
    with pytest.raises(ValueError, match="positive"):
        balancer.set_weights({"A": 0, "B": 0})


def test_weighted_balancer_unhealthy_backend_handling():
    """Kiểm tra bỏ qua backend unhealthy và khôi phục khi healthy trở lại."""
    balancer = WeightedBalancer(
        backends=["A", "B", "C"],
        initial_weights={"A": 3, "B": 1, "C": 1},
    )

    # Đánh dấu A bị lỗi
    balancer.set_backend_health("A", False)
    assert "A" not in balancer.get_active_backends()

    # Chỉ còn B và C chia đều 1:1
    picks = [balancer.pick_nowait() for _ in range(6)]
    assert "A" not in picks
    assert picks.count("B") == 3
    assert picks.count("C") == 3

    # Phục hồi A khỏe mạnh
    balancer.set_backend_health("A", True)
    assert "A" in balancer.get_active_backends()

    picks_recovered = [balancer.pick_nowait() for _ in range(5)]
    assert "A" in picks_recovered


def test_weighted_balancer_in_flight_tracking():
    """Kiểm tra đếm và dọn dẹp in-flight requests."""
    balancer = WeightedBalancer(backends=["A", "B"])
    assert balancer.get_in_flight() == {"A": 0, "B": 0}

    picked = balancer.pick_nowait()
    assert picked is not None
    assert balancer.get_in_flight()[picked] == 1

    balancer.release(picked)
    assert balancer.get_in_flight()[picked] == 0

    # Không bao giờ âm dù gọi release nhiều lần
    balancer.release(picked)
    assert balancer.get_in_flight()[picked] == 0


@pytest.mark.asyncio
async def test_weighted_balancer_async_pick():
    """Kiểm tra phương thức pick() và on_request_end() bất đồng bộ."""
    balancer = WeightedBalancer(backends=["A", "B"], initial_weights={"A": 2, "B": 1})
    picked = await balancer.pick()
    assert picked in ("A", "B")
    assert balancer.get_in_flight()[picked] == 1

    await balancer.on_request_end(picked)
    assert balancer.get_in_flight()[picked] == 0


def test_create_balancer_factory():
    """Kiểm tra factory create_balancer hỗ trợ policy 'weighted' và các alias."""
    b1 = create_balancer("weighted", ["A", "B"])
    assert b1.policy_name == "weighted"

    b2 = create_balancer("wrr", ["A", "B"])
    assert b2.policy_name == "weighted"

    b3 = create_balancer("weighted_round_robin", ["A", "B"])
    assert b3.policy_name == "weighted"
