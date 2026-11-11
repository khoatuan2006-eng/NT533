"""The static caller can balance by in-flight load; the lab variants map to real settings."""

import asyncio
import sys
from pathlib import Path

import pytest

from grpc_lb.application import ServiceSettings
from grpc_lb.dependencies import DependencyPoolFactory, StaticDependencyPool
from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.topology import load_topology

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import labenv  # noqa: E402


class SlowStub:
    def __init__(self, delay, log, name):
        self.delay, self.log, self.name = delay, log, name

    async def Execute(self, request, **kwargs):
        self.log.append(self.name)
        await asyncio.sleep(self.delay)
        return pb.TaskResult(instance_id=self.name)


async def test_least_request_avoids_the_slow_backend_and_releases_counts():
    pool = StaticDependencyPool("worker", ["fast:1", "slow:1"], policy="least_request")
    log = []
    pool.stubs = {"fast:1": SlowStub(0.001, log, "fast"), "slow:1": SlowStub(0.1, log, "slow")}
    pool.balancer.set_active_backends(["fast:1", "slow:1"])
    gate = asyncio.Semaphore(4)

    async def one(i):
        async with gate:
            await pool.call(pb.TaskRequest(request_id=str(i)), 5)

    await asyncio.gather(*(one(i) for i in range(40)))
    assert log.count("slow") < log.count("fast")
    assert pool.balancer.total_in_flight() == 0


async def test_failed_call_still_releases_in_flight_count():
    class Failing:
        async def Execute(self, request, **kwargs):
            raise RuntimeError("boom")

    pool = StaticDependencyPool("worker", ["one:1"], policy="least_request")
    pool.stubs = {"one:1": Failing()}
    pool.balancer.set_active_backends(["one:1"])
    with pytest.raises(RuntimeError):
        await pool.call(pb.TaskRequest(request_id="x"), 5)
    assert pool.balancer.total_in_flight() == 0


def test_unknown_policy_is_rejected_at_startup():
    with pytest.raises(ValueError):
        DependencyPoolFactory("static", static_policy="random")
    entry, topology = load_topology("configs/topology.local.json")
    env = {"TOPOLOGY_PATH": "configs/topology.local.json", "STATIC_LB_POLICY": "random"}
    with pytest.raises(ValueError):
        ServiceSettings.from_environment(env)
    ok = ServiceSettings.from_environment({**env, "STATIC_LB_POLICY": "least_request"})
    assert ok.static_lb_policy == "least_request" and entry in topology


def test_lab_variants_are_consistent():
    for mode, static_policy, ingress_policy in labenv.VARIANTS.values():
        assert mode in ("static", "xds")
        assert static_policy in ("round_robin", "least_request")
        assert ingress_policy in ("round_robin", "least_request")
        # gRPC Python cannot apply least_request received over xDS.
        assert mode == "static" or static_policy == "round_robin"
