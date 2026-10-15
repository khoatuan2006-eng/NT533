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


from grpc_lb.q_learning import QLearningAgent


def test_q_learning_agent_initialization():
    """Kiểm tra khởi tạo Agent và bắt lỗi tham số không hợp lệ."""
    agent = QLearningAgent(backends=["r1", "r2", "r3"], alpha=0.2, gamma=0.8, epsilon=0.05)
    assert agent.backends == ["r1", "r2", "r3"]
    assert agent.num_actions == 4  # 1 equal + 3 boost

    with pytest.raises(ValueError, match="empty"):
        QLearningAgent(backends=[])

    with pytest.raises(ValueError, match="Alpha"):
        QLearningAgent(backends=["r1"], alpha=0.0)

    with pytest.raises(ValueError, match="Gamma"):
        QLearningAgent(backends=["r1"], gamma=1.5)

    with pytest.raises(ValueError, match="Epsilon"):
        QLearningAgent(backends=["r1"], epsilon=-0.1)


def test_q_learning_agent_action_weights():
    """Kiểm tra chuyển đổi action sang bộ trọng số tương ứng."""
    agent = QLearningAgent(backends=["r1", "r2", "r3"], boost_weight=3.0, base_weight=1.0)

    # Action 0: Equal
    assert agent.get_action_weights(0) == {"r1": 1.0, "r2": 1.0, "r3": 1.0}

    # Action 1: Boost r1
    assert agent.get_action_weights(1) == {"r1": 3.0, "r2": 1.0, "r3": 1.0}

    # Action 2: Boost r2
    assert agent.get_action_weights(2) == {"r1": 1.0, "r2": 3.0, "r3": 1.0}

    # Action 3: Boost r3
    assert agent.get_action_weights(3) == {"r1": 1.0, "r2": 1.0, "r3": 3.0}

    # Action không hợp lệ
    with pytest.raises(ValueError, match="out of bounds"):
        agent.get_action_weights(4)


def test_q_learning_agent_epsilon_greedy_selection():
    """Kiểm tra lựa chọn hành động khai thác (epsilon=0) và khám phá (epsilon=1)."""
    agent = QLearningAgent(backends=["r1", "r2", "r3"], epsilon=0.0, seed=42)
    state = ("balanced", 0, 0)

    # Khi ban đầu tất cả Q=0, tie-break chọn hợp lệ
    action = agent.choose(state)
    assert 0 <= action < agent.num_actions

    # Đặt giá trị Q cao nhất cho Action 2
    agent._q_table[agent._state_key(state)] = [0.1, 0.2, 5.0, 0.3]
    # Với epsilon=0, luôn luôn chọn action 2
    for _ in range(10):
        assert agent.choose(state, epsilon=0.0) == 2

    # Với epsilon=1.0 (khám phá tuyệt đối), chọn ngẫu nhiên tất cả các action
    picks = {agent.choose(state, epsilon=1.0) for _ in range(50)}
    assert len(picks) > 1


def test_q_learning_agent_bellman_update():
    """Kiểm tra tính toán cập nhật Q theo đúng công thức Bellman."""
    agent = QLearningAgent(
        backends=["r1", "r2"],
        alpha=0.5,
        gamma=0.8,
    )
    s = ("latency", 1)
    s_next = ("latency", 0)

    # Ban đầu Q(s, 1) = 0.0, max Q(s_next) = 0.0, reward = 10.0
    # td_target = 10.0 + 0.8 * 0.0 = 10.0
    # Q(s, 1) = 0.0 + 0.5 * (10.0 - 0.0) = 5.0
    new_q = agent.update(state=s, action=1, reward=10.0, next_state=s_next)
    assert math.isclose(new_q, 5.0)

    # Thiết lập Q(s_next, 0) = 2.0 (làm max_next_q = 2.0)
    agent.update(state=s_next, action=0, reward=4.0, next_state=s_next)
    # Lần cập nhật thứ 2 cho Q(s, 1):
    # current_q = 5.0
    # max_next_q = 2.0
    # reward = 2.0
    # td_target = 2.0 + 0.8 * 2.0 = 3.6
    # Q(s, 1) = 5.0 + 0.5 * (3.6 - 5.0) = 5.0 - 0.7 = 4.3
    updated_q = agent.update(state=s, action=1, reward=2.0, next_state=s_next)
    assert math.isclose(updated_q, 4.3)


def test_q_learning_agent_state_preset_isolation():
    """Kiểm tra trạng thái của các reward preset khác nhau không bị ghi đè."""
    agent = QLearningAgent(backends=["r1", "r2"])
    state_balanced = ("balanced", 0, 1)
    state_reliability = ("reliability", 0, 1)

    agent.update(state_balanced, action=0, reward=10.0, next_state=state_balanced)
    agent.update(state_reliability, action=0, reward=-5.0, next_state=state_reliability)

    q_table = agent.get_q_table()
    key_bal = agent._state_key(state_balanced)
    key_rel = agent._state_key(state_reliability)

    assert key_bal != key_rel
    assert q_table[key_bal][0] > 0.0
    assert q_table[key_rel][0] < 0.0


def test_q_learning_agent_checkpoint_save_and_load(tmp_path):
    """Kiểm tra lưu checkpoint nguyên tử ra file và nạp lại tương thích."""
    ckpt_file = tmp_path / "q_checkpoint.json"
    agent = QLearningAgent(backends=["r1", "r2", "r3"], alpha=0.3, seed=123)

    state = ("balanced", 1, 0)
    agent.update(state, action=2, reward=8.5, next_state=state)
    original_table = agent.get_q_table()

    # Lưu checkpoint
    agent.save_checkpoint(ckpt_file)
    assert ckpt_file.exists()

    # Tạo agent mới cùng backends và nạp lại
    new_agent = QLearningAgent(backends=["r1", "r2", "r3"])
    new_agent.load_checkpoint(ckpt_file)
    assert new_agent.get_q_table() == original_table


def test_q_learning_agent_checkpoint_incompatibility(tmp_path):
    """Kiểm tra bắt lỗi khi nạp checkpoint không tương thích."""
    ckpt_file = tmp_path / "q_checkpoint.json"
    agent = QLearningAgent(backends=["r1", "r2"])
    agent.save_checkpoint(ckpt_file)

    # 1. Khác danh sách backends
    agent_different_backends = QLearningAgent(backends=["r1", "r3"])
    with pytest.raises(ValueError, match="Incompatible backends"):
        agent_different_backends.load_checkpoint(ckpt_file)

    # 2. File không tồn tại
    with pytest.raises(FileNotFoundError):
        agent.load_checkpoint(tmp_path / "non_existent.json")

