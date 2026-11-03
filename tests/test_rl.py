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
