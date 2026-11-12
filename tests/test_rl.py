import asyncio
import json
import socket
import time

import pytest
from envoy.config.endpoint.v3 import endpoint_pb2

from grpc_lb.controlplane.application import ControlPlane
from grpc_lb.controlplane.resources import EDS, ResourceBuilder
from grpc_lb.controlplane.rl_controller import RLController
from grpc_lb.controlplane.weighted import weighted_snapshot
from grpc_lb.dependencies import XdsDependencyPool
from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.rl import QAgent, reward
from grpc_lb.rl_telemetry import TelemetryCollector


async def test_weights_preserve_membership_and_change_snapshot():
    base = await ResourceBuilder().build({"worker": ["127.0.0.1:1234", "127.0.0.1:1235"]})
    result = weighted_snapshot(base, "worker", {"127.0.0.1:1234": 8, "127.0.0.1:1235": 2})
    assignment = endpoint_pb2.ClusterLoadAssignment()
    assert result.resources[EDS]["worker"].Unpack(assignment)
    assert [x.load_balancing_weight.value for x in assignment.endpoints] == [8, 2]
    assert result.version != base.version
    with pytest.raises(ValueError):
        weighted_snapshot(base, "worker", {"127.0.0.1:1234": 8, "127.0.0.1:9999": 2})


def test_agent_learns_and_untrained_defaults_to_hold():
    agent = QAgent(alpha=1, gamma=0)
    state = (1, 2)
    assert agent.choose(state) == 0
    agent.update(state, 1, 0.8, state)
    assert agent.choose(state) == 1
    assert reward(100, 90, 10, 100, 100, 0) < reward(100, 100, 0, 100, 100, 0)
    assert reward(100, 100, 0, 40, 50, 0, mean_ms=20) > reward(100, 100, 0, 40, 50, 0, mean_ms=30)


def test_load_model_freezes_policy(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"kind": "lookaside-q-v1", "q": [{"state": [0, 5], "values": [0, 0, 1]}]}))
    controller = RLController(str(tmp_path / "telemetry"), "worker", model_path=str(path))
    assert not controller.learning
    assert controller.agent.choose((0, 5)) == 1


async def test_rl_controller_falls_back_on_stale_or_changed_membership(tmp_path):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = RLController(str(path), "worker")
    data = {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": 1,
        "endpoints": {endpoints[0]: [2, 3, 4], endpoints[1]: [10, 11, 12]},
        "offered": 6,
        "errors": 0,
    }
    path.write_text(json.dumps(data))
    weighted = controller.apply(base)
    assert weighted.version != base.version
    assert controller.sequence == 1
    assert controller.apply(base).version == weighted.version
    controller.learning = False
    before = {key: list(values) for key, values in controller.agent.q.items()}
    data["sequence"] = 2
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert all(controller.agent.q[key] == values for key, values in before.items())
    data["timestamp"] = time.time() - 100
    path.write_text(json.dumps(data))
    assert controller.apply(base).version == base.version
    assert controller.pending is None
    changed = await ResourceBuilder().build({"worker": endpoints[:1]})
    assert controller.apply(changed).version == changed.version


async def test_rl_requires_two_consistent_windows_before_change(tmp_path):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = RLController(str(path), "worker")
    controller.learning = False
    controller.agent.choose = lambda *args, **kwargs: 1
    data = {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": 1,
        "endpoints": {endpoints[0]: [2, 3, 4], endpoints[1]: [10, 11, 12]},
        "offered": 6,
        "errors": 0,
    }
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert controller.weight == 5
    data["sequence"] = 2
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert controller.weight == 6
    data["sequence"] = 3
    data["endpoints"] = {endpoints[0]: [10, 11, 12], endpoints[1]: [2, 3, 4]}
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert controller.weight == 6


async def test_real_caller_telemetry_window_is_written_atomically(tmp_path):
    path = tmp_path / "window.json"
    collector = TelemetryCollector(
        "worker", {"one": "127.0.0.1:1234", "two": "127.0.0.1:1235"}, str(path), interval=0.05
    )
    collector.start()
    collector.observe("one", 12)
    collector.observe("two", 25)
    collector.observe(None, 30)
    try:
        async with asyncio.timeout(2):
            while not path.exists():
                await asyncio.sleep(0.01)
        data = json.loads(path.read_text())
        assert data["offered"] == 3 and data["errors"] == 1
        assert data["endpoints"] == {"127.0.0.1:1234": [12], "127.0.0.1:1235": [25]}
    finally:
        await collector.close()


async def test_xds_pool_observes_successful_rpc(tmp_path):
    class Stub:
        async def Execute(self, request, **kwargs):
            return pb.TaskResult(instance_id="one")

    collector = TelemetryCollector("worker", {"one": "127.0.0.1:1234"}, str(tmp_path / "w"))
    pool = XdsDependencyPool.__new__(XdsDependencyPool)
    pool.stub = Stub()
    pool.telemetry = collector
    result = await pool.call(pb.TaskRequest(request_id="one"), timeout=1)
    assert result.instance_id == "one"
    assert len(collector.samples["127.0.0.1:1234"]) == 1


async def test_xds_pool_uses_server_duration_trailer(tmp_path):
    class Call:
        def __await__(self):
            async def result():
                return pb.TaskResult(instance_id="one")

            return result().__await__()

        async def trailing_metadata(self):
            return (("x-server-duration-ms", "13.25"),)

    class Stub:
        def Execute(self, request, **kwargs):
            return Call()

    collector = TelemetryCollector("worker", {"one": "127.0.0.1:1234"}, str(tmp_path / "w"))
    pool = XdsDependencyPool.__new__(XdsDependencyPool)
    pool.stub = Stub()
    pool.telemetry = collector
    await pool.call(pb.TaskRequest(request_id="one"), timeout=1)
    assert collector.samples["127.0.0.1:1234"] == [13.25]


async def test_running_controlplane_publishes_rl_then_expires_to_baseline(tmp_path):
    class Source:
        def __init__(self):
            self.changed = asyncio.Event()
            self.closed = False

        async def read(self):
            return {"worker": ["127.0.0.1:1234", "127.0.0.1:1235"]}

        async def close(self):
            self.closed = True

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    source = Source()
    path = tmp_path / "telemetry.json"
    controller = RLController(str(path), "worker", max_age=0.3)
    data = {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": 1,
        "endpoints": {"127.0.0.1:1234": [2, 3, 4], "127.0.0.1:1235": [10, 11, 12]},
        "offered": 6,
        "errors": 0,
    }
    path.write_text(json.dumps(data))
    app = ControlPlane(f"127.0.0.1:{port}", source, controller)
    stop = asyncio.Event()
    task = asyncio.create_task(app.run(stop))
    try:
        async with asyncio.timeout(3):
            while not app.store.snapshot:
                await asyncio.sleep(0.01)
        weighted_version = app.store.snapshot.version
        await asyncio.sleep(0.35)
        source.changed.set()
        async with asyncio.timeout(3):
            while app.store.snapshot.version == weighted_version:
                await asyncio.sleep(0.01)
        baseline = await ResourceBuilder().build(await source.read())
        assert app.store.snapshot.version == baseline.version
    finally:
        stop.set()
        await task
    assert source.closed


async def test_endpoint_errors_are_written_per_endpoint(tmp_path):
    path = tmp_path / "window.json"
    collector = TelemetryCollector("worker", {"one": "127.0.0.1:1234", "two": "127.0.0.1:1235"}, str(path))
    collector.observe("one", 12)
    collector.observe("one", 40, ok=False)
    collector.observe(None, 30, ok=False)
    await collector.flush()
    data = json.loads(path.read_text())
    assert data["offered"] == 3 and data["errors"] == 2
    assert data["endpoint_errors"] == {"127.0.0.1:1234": 1, "127.0.0.1:1235": 0}


async def test_xds_pool_attributes_error_to_identified_instance(tmp_path):
    import grpc

    class Stub:
        def __init__(self, trailers):
            self.trailers = trailers

        async def Execute(self, request, **kwargs):
            raise grpc.aio.AioRpcError(
                grpc.StatusCode.UNAVAILABLE, grpc.aio.Metadata(), grpc.aio.Metadata(*self.trailers)
            )

    collector = TelemetryCollector("worker", {"one": "127.0.0.1:1234"}, str(tmp_path / "w"))
    pool = XdsDependencyPool.__new__(XdsDependencyPool)
    pool.telemetry = collector
    for trailers in ((("x-instance-id", "one"),), ()):
        pool.stub = Stub(trailers)
        with pytest.raises(grpc.aio.AioRpcError):
            await pool.call(pb.TaskRequest(request_id="one"), timeout=1)
    assert collector.failed == {"127.0.0.1:1234": 1} and collector.errors == 2


async def test_rl_controller_penalizes_endpoint_error_rate(tmp_path):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = RLController(str(path), "worker")
    data = {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": 1,
        "endpoints": {endpoints[0]: [10] * 10, endpoints[1]: [8] * 10},
        "offered": 20,
        "errors": 0,
    }
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert controller.last_category == 1
    # Same latencies, but the first endpoint now fails 2 of 12 calls.
    data.update(sequence=2, offered=22, errors=2, endpoint_errors={endpoints[0]: 2})
    path.write_text(json.dumps(data))
    controller.apply(base)
    assert controller.last_category == 2
    data.update(sequence=3, endpoint_errors={endpoints[0]: 3})
    path.write_text(json.dumps(data))
    assert controller.apply(base).version == base.version


def _window(endpoints, fast_first, sequence):
    fast, slow = [2, 3, 4], [10, 11, 12]
    return {
        "timestamp": time.time(),
        "service": "worker",
        "sequence": sequence,
        "endpoints": dict(zip(endpoints, (fast, slow) if fast_first else (slow, fast), strict=True)),
        "offered": 6,
        "errors": 0,
    }


async def test_rule_policy_shares_safety_and_moves_towards_the_faster_endpoint(tmp_path, caplog):
    endpoints = ["127.0.0.1:1234", "127.0.0.1:1235"]
    base = await ResourceBuilder().build({"worker": endpoints})
    path = tmp_path / "telemetry.json"
    controller = RLController(str(path), "worker", policy="rule")
    assert not controller.learning
    caplog.set_level("INFO", logger="grpc_lb.controlplane.rl_controller")
    for sequence in range(1, 7):
        path.write_text(json.dumps(_window(endpoints, True, sequence)))
        controller.apply(base)
    # Hysteresis holds the first window, then one step per window up to the 70% bound.
    assert controller.weight == 7
    windows = [json.loads(r.message) for r in caplog.records if '"rl_window"' in r.message]
    assert windows[-1]["policy"] == "rule" and windows[-1]["weights"] == {endpoints[0]: 70, endpoints[1]: 30}
    path.write_text(json.dumps({**_window(endpoints, True, 7), "timestamp": time.time() - 100}))
    assert controller.apply(base).version == base.version
    controller.apply(base)
    fallbacks = [r for r in caplog.records if '"rl_fallback"' in r.message]
    assert len(fallbacks) == 1  # logged once per reason, not on every publication


def test_rule_policy_rejects_a_model_and_unknown_policies(tmp_path):
    with pytest.raises(ValueError):
        RLController(str(tmp_path / "t"), "worker", policy="rule", model_path=str(tmp_path / "m"))
    with pytest.raises(ValueError):
        RLController(str(tmp_path / "t"), "worker", policy="bandit")


def test_controlplane_settings_read_the_policy():
    from grpc_lb.controlplane.config import RuntimeSettings

    base = {"EXPERIMENTAL_RL_TELEMETRY": "/rl/t.json", "EXPERIMENTAL_RL_SERVICE": "service-b"}
    assert RuntimeSettings.from_environment(base).rl_policy == "q"
    assert RuntimeSettings.from_environment({**base, "EXPERIMENTAL_RL_POLICY": "rule"}).rl_policy == "rule"
    # Empty values, as Compose passes them for the variants without a controller.
    empty = {"EXPERIMENTAL_RL_TELEMETRY": "", "EXPERIMENTAL_RL_SERVICE": "", "EXPERIMENTAL_RL_POLICY": ""}
    assert RuntimeSettings.from_environment(empty).rl_service == ""
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment(
            {**base, "EXPERIMENTAL_RL_POLICY": "rule", "EXPERIMENTAL_RL_MODEL": "m"}
        )
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment({**base, "EXPERIMENTAL_RL_POLICY": "x"})


def test_shipped_model_loads_in_the_shape_controller():
    from pathlib import Path

    from grpc_lb.controlplane.rl_controller import ShapeController

    model = Path(__file__).resolve().parents[1] / "configs/shape-model.json"
    controller = ShapeController("unused", "worker", policy="shape-q", model_path=str(model))
    assert controller.core.bandit.counts and controller.core.beta == 2.0 and controller.core.dwell == 5


async def test_rpc_latency_mode_ignores_the_server_duration_trailer(tmp_path):
    class Call:
        def __await__(self):
            async def result():
                return pb.TaskResult(instance_id="one")

            return result().__await__()

        async def trailing_metadata(self):
            return (("x-server-duration-ms", "0.5"),)

    class Stub:
        def Execute(self, request, **kwargs):
            return Call()

    collector = TelemetryCollector("worker", {"one": "127.0.0.1:1234"}, str(tmp_path / "w"), latency="rpc")
    pool = XdsDependencyPool.__new__(XdsDependencyPool)
    pool.stub = Stub()
    pool.telemetry = collector
    await pool.call(pb.TaskRequest(request_id="one"), timeout=1)
    [sample] = collector.samples["127.0.0.1:1234"]
    assert sample != 0.5
    with pytest.raises(ValueError):
        TelemetryCollector("worker", {}, str(tmp_path / "w"), latency="network")


def test_service_settings_validate_the_latency_source():
    from grpc_lb.application import ServiceSettings

    env = {"TOPOLOGY_PATH": "configs/topology.local.json"}
    assert ServiceSettings.from_environment(env).rl_telemetry_latency == "server"
    assert (
        ServiceSettings.from_environment({**env, "EXPERIMENTAL_RL_LATENCY": "rpc"}).rl_telemetry_latency
        == "rpc"
    )
    with pytest.raises(ValueError):
        ServiceSettings.from_environment({**env, "EXPERIMENTAL_RL_LATENCY": "wire"})
