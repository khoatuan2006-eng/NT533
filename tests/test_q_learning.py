from __future__ import annotations

import asyncio
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


from grpc_lb.client import WorkloadClient
from grpc_lb.contracts import CallResult, CallStatus


@pytest.mark.asyncio
async def test_client_q_learning_policy_binding():
    """Kiểm tra WorkloadClient tự động khởi tạo WeightedBalancer khi chọn policy q_learning."""
    backends = [
        {"id": "replica-1", "address": "127.0.0.1:50051"},
        {"id": "replica-2", "address": "127.0.0.1:50052"},
    ]
    client = WorkloadClient(backends=backends, policy="q_learning")

    try:
        assert client.policy == "q_learning"
        assert client.balancer.policy_name == "weighted"

        # Kiểm tra get_in_flight ban đầu
        assert client.get_in_flight() == {"replica-1": 0, "replica-2": 0}
        assert set(client.get_active_backends()) == {"replica-1", "replica-2"}

        # Kiểm tra set_weights và get_weights
        client.set_weights({"replica-1": 3.0, "replica-2": 1.0})
        assert client.get_weights() == {"replica-1": 3.0, "replica-2": 1.0}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_set_weights_unsupported_policy():
    """Kiểm tra gọi set_weights trên policy không hỗ trợ sẽ ném NotImplementedError."""
    backends = [{"id": "replica-1", "address": "127.0.0.1:50051"}]
    client = WorkloadClient(backends=backends, policy="round_robin")
    try:
        with pytest.raises(NotImplementedError, match="không hỗ trợ"):
            client.set_weights({"replica-1": 2.0})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_result_callback_notification():
    """Kiểm tra callback được thông báo khi RPC hoàn tất (kể cả khi không có backend)."""
    backends = [{"id": "replica-1", "address": "127.0.0.1:50051"}]
    client = WorkloadClient(backends=backends, policy="q_learning")

    received_results: list[CallResult] = []
    client.add_result_callback(lambda res: received_results.append(res))

    try:
        # Giả lập backend bị un-healthy -> execute trả về NO_BACKEND
        client.balancer.set_backend_health("replica-1", False)

        result = await client.execute("run-1", "req-1", timeout_s=1.0)
        assert result.status == CallStatus.NO_BACKEND
        assert len(received_results) == 1
        assert received_results[0].request_id == "req-1"
        assert received_results[0].policy == "q_learning"

        # Thử gỡ callback
        client.remove_result_callback(client._result_callbacks[0])
        await client.execute("run-1", "req-2", timeout_s=1.0)
        # Số kết quả trong danh sách không đổi vì đã gỡ callback
        assert len(received_results) == 1
    finally:
        await client.close()


def test_q_learning_closed_loop_convergence():
    """Kiểm chứng vòng lặp học khép kín: Agent tự động nhận diện action tối ưu và cập nhật trọng số.

    Kịch bản:
      - 3 replica: replica-1 là máy chủ tối ưu (nhanh nhất).
      - Action 1 (boost replica-1) nhận điểm thưởng cao (+10.0).
      - Action 0 (equal) nhận điểm trung bình (+1.0).
      - Action 2 & 3 (boost replica 2 hoặc 3) nhận điểm phạt (-5.0).
      - Sau 50 bước học, Agent phải hội tụ vào Action 1 khi khai thác (epsilon=0.0).
    """
    backends = ["replica-1", "replica-2", "replica-3"]
    agent = QLearningAgent(backends=backends, alpha=0.3, gamma=0.8, epsilon=0.2, seed=42)
    balancer = WeightedBalancer(backends=backends)

    state = ("balanced", 0)

    for _ in range(60):
        action = agent.choose(state)
        # Áp trọng số xuống balancer
        weights = agent.get_action_weights(action)
        balancer.set_weights(weights)

        # Môi trường phản hồi điểm thưởng tương ứng
        if action == 1:
            reward = 10.0
        elif action == 0:
            reward = 1.0
        else:
            reward = -5.0

        # Cập nhật Bellman
        agent.update(state, action, reward, state)

    # Sau quá trình học, kiểm tra chế độ khai thác (exploitation)
    best_action = agent.choose(state, epsilon=0.0)
    assert best_action == 1, f"Kỳ vọng Agent hội tụ vào Action 1 (boost replica-1), nhưng được {best_action}"

    optimal_weights = agent.get_action_weights(best_action)
    assert optimal_weights["replica-1"] == 3.0
    assert optimal_weights["replica-2"] == 1.0
    assert optimal_weights["replica-3"] == 1.0

    # Phân phối tải thực tế qua balancer phải phản ánh tỉ lệ 3:1:1
    balancer.set_weights(optimal_weights)
    picks = [balancer.pick_nowait() for _ in range(5)]
    assert picks.count("replica-1") == 3
    assert picks.count("replica-2") == 1
    assert picks.count("replica-3") == 1


@pytest.mark.asyncio
async def test_weighted_balancer_in_flight_cleanup_under_cancelled_error():
    """Kiểm tra bộ đếm in-flight của WeightedBalancer luôn được hoàn trả về 0 khi tác vụ bị Cancel."""
    balancer = WeightedBalancer(backends=["replica-1", "replica-2"])

    # Mô phỏng coroutine lấy replica và bị cancel giữa chừng
    picked = balancer.pick_nowait()
    assert picked == "replica-1"
    assert balancer.get_in_flight()["replica-1"] == 1

    try:
        # Giả lập khối try...finally như trong client.execute
        raise asyncio.CancelledError("Tác vụ bị ngắt đột ngột")
    except asyncio.CancelledError:
        pass
    finally:
        balancer.release(picked)

    # Đảm bảo in-flight trở về 0 an toàn
    assert balancer.get_in_flight()["replica-1"] == 0
    assert balancer.get_in_flight()["replica-2"] == 0


def test_q_learning_with_dynamic_backend_failure_and_recovery():
    """Kiểm tra phân phối trọng số thích ứng khi một replica bị rớt mạng và khôi phục."""
    backends = ["A", "B", "C"]
    balancer = WeightedBalancer(backends=backends, initial_weights={"A": 3.0, "B": 2.0, "C": 1.0})

    # Ban đầu cả 3 đều sống, tổng trọng số là 6
    picks_initial = [balancer.pick_nowait() for _ in range(6)]
    assert picks_initial.count("A") == 3
    assert picks_initial.count("B") == 2
    assert picks_initial.count("C") == 1

    # Backend A gặp sự cố
    balancer.set_backend_health("A", False)
    assert balancer.get_active_backends() == ["B", "C"]

    # Chỉ còn B và C phân phối theo tỉ lệ 2:1
    picks_fail = [balancer.pick_nowait() for _ in range(3)]
    assert "A" not in picks_fail
    assert picks_fail.count("B") == 2
    assert picks_fail.count("C") == 1

    # Backend A phục hồi
    balancer.set_backend_health("A", True)
    picks_recovered = [balancer.pick_nowait() for _ in range(6)]
    assert picks_recovered.count("A") == 3
    assert picks_recovered.count("B") == 2
    assert picks_recovered.count("C") == 1


