"""RL version 2: weight formula, decision core, controller safety and the queueing simulator."""

import json
import random
import sys
import time
from pathlib import Path

import pytest

from grpc_lb.controlplane.config import RuntimeSettings
from grpc_lb.controlplane.resources import ResourceBuilder
from grpc_lb.controlplane.rl_controller import ShapeController, queue_share
from grpc_lb.rl import (
    BETAS,
    QAgent,
    ShapeCore,
    percent_weights,
    relative_reward,
    shape_context,
    shape_weights,
    step_towards,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import train_shape_rl  # noqa: E402


def test_beta_zero_is_round_robin_and_larger_beta_favours_the_fast_endpoint():
    assert shape_weights([10, 20, 60], 0, 0.05) == pytest.approx([1 / 3] * 3)
    mild, sharp = shape_weights([10, 20, 60], 0.5, 0.05), shape_weights([10, 20, 60], 4, 0.05)
    assert mild[0] < sharp[0] and min(sharp) >= 0.05 - 1e-12 and sum(sharp) == pytest.approx(1)
    with pytest.raises(ValueError):
        shape_weights([10], 1, 0.05)
    with pytest.raises(ValueError):
        shape_weights([10, 20, 30], 1, 0.4)  # three endpoints cannot each keep 40%


def test_percent_weights_sum_to_100_and_stay_valid_for_xds():
    for fractions in ([1 / 3] * 3, [0.995, 0.005], [0.7, 0.2, 0.05, 0.05]):
        weights = percent_weights(fractions)
        assert sum(weights) == 100 and all(1 <= w <= 100 for w in weights)


def test_step_towards_moves_at_most_max_step_and_keeps_the_floor():
    weights = [34, 33, 33]
    for _ in range(10):
        new = step_towards(weights, [90, 5, 5], 10, 0.05)
        assert sum(new) == 100 and max(abs(a - b) for a, b in zip(new, weights, strict=True)) <= 11
        weights = new
    assert weights == [90, 5, 5]


def test_context_buckets_spread_and_pressure():
    assert shape_context([10, 11], 0.0) == (0, 0)
    assert shape_context([10, 50], 0.5) == (3, 2)


def test_relative_reward_cancels_the_offered_load():
    # Same 10% improvement at light and heavy load gives the same reward.
    assert relative_reward(9, 10, 0, 0) == pytest.approx(relative_reward(90, 100, 0, 0))
    assert relative_reward(10, 10, 0.05, 0) < relative_reward(10, 10, 0, 0)


def test_core_with_agent_changes_beta_only_after_two_consistent_windows():
    agent = QAgent(epsilon=0)
    agent.choose = lambda state, **kwargs: 1
    core = ShapeCore(agent=agent)
    core.reset(3)
    start = core.beta_index
    core.step([10, 20, 60], 0.0)
    assert core.beta_index == start
    core.step([10, 20, 60], 0.0)
    assert core.beta_index == start + 1
    fixed = ShapeCore(fixed_beta=2.0)
    fixed.reset(3)
    fixed.step([10, 20, 60], 0.0)
    assert fixed.beta == 2.0 and fixed.weights[0] > fixed.weights[2]
    with pytest.raises(ValueError):
        ShapeCore(agent=agent, fixed_beta=1.0)


def _window(endpoints, latencies, sequence, queues=None):
    data = {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": sequence,
        "endpoints": {address: [value] * 5 for address, value in zip(endpoints, latencies, strict=True)},
        "offered": 5 * len(endpoints),
        "errors": 0,
    }
    if queues is not None:
        data["endpoint_queue_ms"] = {
            address: [value] * 5 for address, value in zip(endpoints, queues, strict=True)
        }
    return data


async def test_shape_controller_moves_weights_and_falls_back_on_bad_data(tmp_path):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235", "127.0.0.1:1236"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = ShapeController(str(path), "worker", policy="shape-fixed", beta=4)
    for sequence in range(1, 8):
        path.write_text(json.dumps(_window(endpoints, [10, 20, 60], sequence)))
        weighted = controller.apply(base)
    assert weighted.version != base.version
    assert controller.core.weights[0] > 70 and controller.core.weights[2] == 5
    path.write_text(json.dumps({**_window(endpoints, [10, 20, 60], 8), "timestamp": time.time() - 100}))
    assert controller.apply(base).version == base.version
    assert controller.core.weights == []  # restarts from even weights once data is good again


def test_shape_controller_rejects_inconsistent_settings(tmp_path):
    with pytest.raises(ValueError):
        ShapeController(str(tmp_path / "t"), "worker", policy="shape-q")
    with pytest.raises(ValueError):
        ShapeController(str(tmp_path / "t"), "worker", policy="shape-fixed")
    model = tmp_path / "model.json"
    model.write_text(json.dumps({"kind": "lookaside-shape-v1", "betas": [1, 2], "q": []}))
    with pytest.raises(ValueError):
        ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(model))
    model.write_text(json.dumps({"kind": "lookaside-shape-v1", "betas": list(BETAS), "q": []}))
    ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(model))


def test_queue_share_weights_by_traffic_and_tolerates_missing_reports():
    endpoints = ["a:1", "b:1"]
    assert queue_share(_window(endpoints, [10, 10], 1), endpoints, [50, 50]) == 0
    data = _window(endpoints, [10, 10], 1, queues=[5, 0])
    assert queue_share(data, endpoints, [100, 0]) == pytest.approx(0.5)
    assert queue_share(data, endpoints, [50, 50]) == pytest.approx(0.25)


def test_settings_accept_shape_policies():
    base = {"EXPERIMENTAL_RL_TELEMETRY": "/rl/t.json", "EXPERIMENTAL_RL_SERVICE": "service-b"}
    fixed = RuntimeSettings.from_environment(
        {**base, "EXPERIMENTAL_RL_POLICY": "shape-fixed", "EXPERIMENTAL_RL_BETA": "2"}
    )
    assert fixed.rl_beta == "2"
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment({**base, "EXPERIMENTAL_RL_POLICY": "shape-fixed"})
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment({**base, "EXPERIMENTAL_RL_POLICY": "shape-q"})
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment({**base, "EXPERIMENTAL_RL_BETA": "2"})


def test_queue_model_waits_grow_with_load_and_overload_is_rejected():
    light, _ = train_shape_rl.erlang_c_wait(4, 10, 0.1)
    heavy, rejected = train_shape_rl.erlang_c_wait(4, 10, 0.38)
    assert 0 <= light < heavy and rejected == 0
    _, overloaded = train_shape_rl.erlang_c_wait(4, 10, 1.0)
    assert overloaded > 0.5


def test_round_robin_scores_zero_against_itself_in_the_simulator():
    rng = random.Random(3)
    for kind in train_shape_rl.KINDS:
        scores = train_shape_rl.run_episode(train_shape_rl.Scenario(rng, kind), ShapeCore(fixed_beta=0))
        assert abs(sum(scores) / len(scores)) < 0.02


def test_training_produces_a_model_the_controller_loads(tmp_path):
    agent = train_shape_rl.train(30, seed=5)
    path = tmp_path / "model.json"
    path.write_text(
        json.dumps(
            {
                "kind": "lookaside-shape-v1",
                "betas": list(BETAS),
                "q": [{"state": list(s), "values": v} for s, v in agent.q.items()],
            }
        )
    )
    controller = ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(path))
    assert controller.core.agent.q


async def test_attributed_errors_shift_load_instead_of_triggering_fallback(tmp_path):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = ShapeController(str(path), "worker", policy="shape-fixed", beta=2)
    window = _window(endpoints, [10, 10], 1)
    # The second endpoint rejected 5 of its 10 calls: 25% errors overall, all attributed.
    window.update(offered=20, errors=5, endpoint_errors={endpoints[1]: 5})
    path.write_text(json.dumps(window))
    assert controller.apply(base).version != base.version
    assert controller.core.weights[0] > controller.core.weights[1]
    window.update(sequence=2, errors=8)  # 3 more errors no endpoint explains: 15% unattributed
    path.write_text(json.dumps(window))
    assert controller.apply(base).version == base.version


def test_start_beta_and_hold_travel_with_the_model(tmp_path):
    agent = QAgent(epsilon=0)
    agent.choose = lambda state, **kwargs: 1
    core = ShapeCore(agent=agent, start_beta=2.0, hold=1)
    core.reset(3)
    assert core.beta == 2.0
    core.step([10, 20, 60], 0.0)
    assert core.beta == 4.0  # hold=1: the first window may already move beta
    with pytest.raises(ValueError):
        ShapeCore(agent=agent, start_beta=3.0)
    model = tmp_path / "model.json"
    model.write_text(
        json.dumps(
            {"kind": "lookaside-shape-v1", "betas": list(BETAS), "q": [], "start_beta": 2.0, "hold": 1}
        )
    )
    controller = ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(model))
    assert controller.core.beta == 2.0 and controller.core.hold == 1
    model.write_text(json.dumps({"kind": "lookaside-shape-v1", "betas": list(BETAS), "q": []}))
    older = ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(model))
    assert older.core.beta == 1.0 and older.core.hold == 2  # defaults it was trained with


def test_bandit_picks_the_best_seen_beta_and_keeps_it_for_dwell_windows():
    from grpc_lb.rl import BanditTable

    table = BanditTable()
    assert table.choose((2, 0), default=3) == 3  # unseen context: trained default
    table.update((2, 0), 4, 0.5)
    table.update((2, 0), 1, 0.2)
    assert table.choose((2, 0), default=3) == 4
    core = ShapeCore(bandit=table, start_beta=2.0, hold=1, dwell=3)
    core.reset(3)
    core.step([10, 20, 60], 0.0)  # spread bucket 3, pressure 0
    table.update((3, 0), 0, 1.0)
    core.step([10, 20, 60], 0.0)
    assert core.beta == 2.0  # the earlier choice stays in force for dwell windows
    for _ in range(3):
        core.step([10, 20, 60], 0.0)
    assert core.beta == 0.0
    with pytest.raises(ValueError):
        ShapeCore(bandit=table, fixed_beta=1.0)


def test_bandit_model_round_trips_through_the_controller(tmp_path):
    table = train_shape_rl.train(
        40, seed=3, decision={"start_beta": 2.0, "hold": 2, "dwell": 3}, method="bandit"
    )
    model = {
        "kind": "lookaside-shape-bandit-v1",
        "betas": list(BETAS),
        "start_beta": 2.0,
        "hold": 2,
        "dwell": 3,
        "contexts": [
            {"context": list(c), "values": table.values[c], "counts": table.counts[c]} for c in table.counts
        ],
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model))
    controller = ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(path))
    assert controller.core.bandit.counts == table.counts and controller.core.dwell == 3
    model["contexts"][0]["counts"] = [-1] * len(BETAS)
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError):
        ShapeController(str(tmp_path / "t"), "worker", policy="shape-q", model_path=str(path))


def test_localhost_scenarios_scale_with_the_backend_count():
    import compare_shape_rl

    # Three backends reproduce the original scenarios.
    assert [s for _, s in compare_shape_rl.scenario_specs("uneven-capacity", 3)] == [8, 4, 2]
    for scenario in compare_shape_rl.SCENARIOS:
        for n in (3, 10):
            specs = compare_shape_rl.scenario_specs(scenario, n)
            assert len(specs) == n
    # About 30% of the backends are affected, and plain capacity stays the same at any size.
    slow = compare_shape_rl.scenario_specs("processing-slow", 10)
    assert sum(delay > compare_shape_rl.base_delay(10) for delay, _ in slow) == 3
    capacity = {n: sum(s / d for d, s in compare_shape_rl.scenario_specs("even", n)) for n in (3, 10)}
    assert capacity[3] == pytest.approx(capacity[10])


def test_simulated_faults_touch_about_30_percent_of_backends():
    rng = random.Random(1)
    for n in (2, 3, 10):
        scenario = train_shape_rl.Scenario(rng, "path-slow", (n,))
        assert len(scenario.slow) == train_shape_rl.affected(n) == max(1, round(0.3 * n))


def test_shrinking_ignores_gaps_within_the_noise_and_keeps_real_ones():
    from grpc_lb.rl import shrink_latencies

    # 10 equal backends measured with 10% noise: everything within 2 SE collapses to equal.
    noisy = [100, 108, 93, 104, 97, 111, 95, 102, 99, 106]
    shrunk = shrink_latencies(noisy, [10.0] * 10)
    assert max(shrunk) == min(shrunk)
    assert shape_weights(shrunk, 4, 0.05) == pytest.approx([0.1] * 10)
    # A real 3x gap survives, reduced by the noise margin.
    shrunk = shrink_latencies([100, 100, 300], [5.0, 5.0, 15.0], k=2)
    assert shrunk[2] == pytest.approx(270) and shrunk[0] == shrunk[1] == 100
    with pytest.raises(ValueError):
        shrink_latencies([1, 2], [1.0])


def test_median_errors_widen_with_fewer_samples():
    from grpc_lb.controlplane.rl_controller import median_errors

    endpoints = ["a:1", "b:1"]
    data = {"endpoints": {"a:1": [90, 110] * 50, "b:1": [90, 110] * 2}}
    many, few = median_errors(data, endpoints, [100.0, 100.0])
    assert 0 < many < few


def test_threshold_grows_with_the_number_of_endpoints():
    from grpc_lb.rl import significance

    assert 2.3 < significance(3) < 2.5 and 2.7 < significance(10) < 2.9
