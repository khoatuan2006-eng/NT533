"""Generic graph integration tests over real gRPC transports."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import replace

import grpc
import pytest
from grpc_health.v1 import health_pb2 as hp

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc
from grpc_lb.microservices import Service, health_name, start_service
from grpc_lb.topology import ServiceConfig, load_topology, validate


@asynccontextmanager
async def cluster(
    mode="sequential", delay=0.001, concurrency=50, timeout=5, failure=0, extra=False, heavy_multiplier=5
):
    # Arbitrary names deliberately differ from the supplied A/B/C example.
    configs = {
        name: ServiceConfig(
            name,
            {f"{name}-{i}": f"{name}-{i}:50051" for i in (1, 2)},
            delay_s=delay,
            heavy_multiplier=heavy_multiplier,
        )
        for name in ("worker-x", "worker-y", "gateway")
    }
    configs["worker-y"] = replace(configs["worker-y"], failure_rate=failure)
    configs["gateway"] = replace(
        configs["gateway"],
        dependencies=("worker-x", "worker-y"),
        mode=mode,
        concurrency=concurrency,
        timeout_s=timeout,
        delay_s=0,
    )
    if extra:
        configs["extra"] = ServiceConfig("extra", {"extra-1": "extra:50051"}, delay_s=0)
        configs["worker-x"] = replace(configs["worker-x"], dependencies=("extra",))
    nodes = {}
    try:
        for name in validate(configs):
            for instance in configs[name].instances:
                runtime = await start_service(
                    configs[name], instance, configs, "127.0.0.1:0", health_interval=0.05
                )
                nodes[instance] = runtime
                configs[name].instances[instance] = f"127.0.0.1:{runtime.port}"
        # Health/connection establishment is asynchronous; make readiness a test
        # precondition instead of assuming a 300ms startup probe always succeeds.
        deadline = time.monotonic() + 5
        for runtime in nodes.values():
            for pool in runtime.pools.values():
                while len(pool.balancer.get_active_backends()) != len(pool.channels):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Test dependency health did not converge")
                    await pool.refresh()
        yield configs, nodes
    finally:
        for runtime in reversed(list(nodes.values())):
            await runtime.close()


def request(index=1, **kwargs):
    values = dict(
        request_id=f"request-{index}", target_service="gateway", workload="light", remaining_hops=16
    )
    values.update(kwargs)
    return pb.TaskRequest(**values)


async def test_injected_workload_runs_under_request_budget():
    class SlowWorkload:
        def __init__(self):
            self.cancelled = False

        async def execute(self, request):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    config = ServiceConfig("gateway", {"one": "gateway:50051"}, timeout_s=0.05)
    workload = SlowWorkload()
    runtime = await start_service(config, "one", {"gateway": config}, "127.0.0.1:0", workload=workload)
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.port}") as channel:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await rpc.ProcessingServiceStub(channel).Execute(request())
            assert error.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        assert workload.cancelled
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sequential", "parallel"])
async def test_generic_graph_and_distribution(mode):
    async with cluster(mode=mode, extra=True) as (configs, nodes):
        for instance in ("gateway-1", "gateway-2"):
            async with grpc.aio.insecure_channel(configs["gateway"].instances[instance]) as channel:
                stub = rpc.ProcessingServiceStub(channel)
                results = [await stub.Execute(request(n), timeout=2) for n in range(8)]
                assert {r.instance_id for r in results} == {instance}
                assert {r.children[0].instance_id for r in results} == {"worker-x-1", "worker-x-2"}
                assert {r.children[1].instance_id for r in results} == {"worker-y-1", "worker-y-2"}
                assert all(r.children[0].children[0].instance_id == "extra-1" for r in results)
                assert [r.request_id for r in results] == [f"request-{n}" for n in range(8)]


@pytest.mark.asyncio
async def test_health_exclusion_and_recovery():
    async with cluster() as (configs, nodes):
        unhealthy = nodes["worker-x-1"].health
        pool = nodes["gateway-1"].pools["worker-x"]
        await unhealthy.set(health_name("worker-x"), hp.HealthCheckResponse.NOT_SERVING)
        await pool.refresh()
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            stub = rpc.ProcessingServiceStub(channel)
            for i in range(4):
                assert (await stub.Execute(request(i), timeout=2)).children[0].instance_id == "worker-x-2"
            await unhealthy.set(health_name("worker-x"), hp.HealthCheckResponse.SERVING)
            await pool.refresh()
            results = [await stub.Execute(request(i), timeout=2) for i in range(4)]
            assert {r.children[0].instance_id for r in results} == {"worker-x-1", "worker-x-2"}


@pytest.mark.asyncio
async def test_health_watch_pushes_status_without_polling():
    async with cluster() as (configs, nodes):
        pool = nodes["gateway-1"].pools["worker-x"]
        address = configs["worker-x"].instances["worker-x-1"]

        async def await_state(expected):
            end = time.monotonic() + 3
            while (address in pool.balancer.get_active_backends()) != expected:
                if time.monotonic() > end:
                    raise AssertionError("Watch did not deliver health change")
                await asyncio.sleep(0.01)

        await nodes["worker-x-1"].health.set(health_name("worker-x"), hp.HealthCheckResponse.NOT_SERVING)
        await await_state(False)
        await nodes["worker-x-1"].health.set(health_name("worker-x"), hp.HealthCheckResponse.SERVING)
        await await_state(True)


@pytest.mark.asyncio
async def test_dependency_down_returns_unavailable():
    async with cluster() as (configs, nodes):
        for instance in ("worker-y-1", "worker-y-2"):
            await nodes[instance].server.stop(0)
        await nodes["gateway-1"].pools["worker-y"].refresh()
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(request(), timeout=2)
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE


@pytest.mark.asyncio
async def test_deadline_releases_capacity():
    async with cluster(delay=0.1, concurrency=1) as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            stub = rpc.ProcessingServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Execute(request(), timeout=0.03)
            assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
            assert (await stub.Execute(request(2), timeout=2)).request_id == "request-2"


@pytest.mark.asyncio
async def test_total_budget_not_reset_between_dependencies():
    async with cluster(delay=0.09, timeout=0.14) as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(request(), timeout=2)
            assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,code",
    [
        ({"request_id": ""}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"target_service": "other"}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"workload": "unknown"}, grpc.StatusCode.INVALID_ARGUMENT),
        ({"remaining_hops": 1}, grpc.StatusCode.RESOURCE_EXHAUSTED),
        ({"visited_services": ["gateway"]}, grpc.StatusCode.FAILED_PRECONDITION),
    ],
)
async def test_invalid_or_recursive_request(fields, code):
    async with cluster() as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(request(**fields), timeout=2)
            assert exc.value.code() == code


@pytest.mark.asyncio
async def test_parallel_branches_overlap_and_failure_cancels_sibling(monkeypatch):
    entered = {name: asyncio.Event() for name in ("worker-x", "worker-y")}
    cancelled = asyncio.Event()
    original = Service.Execute

    async def execute(self, task, context):
        name = self.config.service_id
        if name == "worker-x":
            entered[name].set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        if name == "worker-y":
            entered[name].set()
            # A sequential implementation cannot reach this barrier.
            await asyncio.wait_for(entered["worker-x"].wait(), 1)
            await context.abort(grpc.StatusCode.UNAVAILABLE, "Injected branch failure")
        return await original(self, task, context)

    monkeypatch.setattr(Service, "Execute", execute)
    async with cluster(mode="parallel") as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(request(), timeout=2)
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
            await asyncio.wait_for(cancelled.wait(), 1)
            assert all(event.is_set() for event in entered.values())


@pytest.mark.asyncio
async def test_configured_failure_injection():
    async with cluster(failure=1) as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(request(), timeout=2)
            assert exc.value.code() == grpc.StatusCode.UNAVAILABLE
            assert "Simulated" in exc.value.details()
            # Failures still identify the answering instance for error attribution.
            assert dict(exc.value.trailing_metadata())["x-instance-id"] == "gateway-1"
            # An application rejection is not proof that its endpoint died.
            assert len(nodes["gateway-1"].pools["worker-y"].balancer.get_active_backends()) == 2


@pytest.mark.asyncio
async def test_success_trailers_expose_local_budget_and_duration():
    async with cluster() as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            call = rpc.ProcessingServiceStub(channel).Execute(request(), timeout=2)
            await call
            metadata = dict(list(await call.trailing_metadata()))
            assert 0 <= float(metadata["x-server-queue-ms"]) <= float(metadata["x-server-duration-ms"])
            # The server derives its budget from its own clock; allow coarse-timer skew.
            assert float(metadata["x-server-duration-ms"]) < float(metadata["x-server-budget-ms"]) <= 2050
            assert metadata["x-instance-id"] == "gateway-1"


def test_topology_validation_and_config_files():
    for path in ("configs/topology.local.json", "configs/topology.compose.json"):
        entry, configs = load_topology(path)
        assert entry in configs
    a = ServiceConfig("a", {"a-1": "a:1"}, dependencies=("b",))
    b = ServiceConfig("b", {"b-1": "b:1"}, dependencies=("a",))
    with pytest.raises(ValueError, match="cycle"):
        validate({"a": a, "b": b})
    with pytest.raises(ValueError, match="Unknown"):
        validate({"a": a})
    with pytest.raises(ValueError, match="mode"):
        validate({"a": replace(a, dependencies=(), mode="invalid")})


@pytest.mark.asyncio
async def test_heavy_profile_uses_configured_multiplier():
    async with cluster(delay=0.02, timeout=0.5, heavy_multiplier=50) as (configs, nodes):
        async with grpc.aio.insecure_channel(configs["gateway"].instances["gateway-1"]) as channel:
            stub = rpc.ProcessingServiceStub(channel)
            assert (await stub.Execute(request(), timeout=2)).request_id == "request-1"
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Execute(request(2, workload="heavy"), timeout=2)
            assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
