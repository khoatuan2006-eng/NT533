"""Registry consistency, stale leases, and health independent of registry I/O."""

import asyncio
import time
from contextlib import asynccontextmanager

import grpc
import httpx
import pytest
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from grpc_lb.controlplane.config import REGISTRY_LEASE
from grpc_lb.controlplane.sources import CatalogSource, ConsulRegistry, HealthController
from grpc_lb.service_identity import health_name

POLICY = {"service-b": []}
ROW = {
    "Service": {"ID": "b-1", "Service": "service-b", "Address": "127.0.0.1", "Port": 50101},
    "Checks": [{"Status": "passing", "ServiceID": "b-1", "Type": "grpc"}],
}


@pytest.mark.parametrize("membership_only", [False, True])
async def test_consistent_registry_query(membership_only):
    def handler(request):
        assert "consistent" in request.url.params
        assert ("passing" not in request.url.params) == membership_only
        row = {
            **ROW,
            "Checks": [{**ROW["Checks"][0], "Status": "critical" if membership_only else "passing"}],
        }
        return httpx.Response(200, json=[row])

    source = ConsulRegistry(
        "http://registry",
        membership_only=membership_only,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert await source.read(POLICY) == {"service-b": ["127.0.0.1:50101"]}
    finally:
        await source.close()


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        [None],
        [{"Service": ROW["Service"]}],
        [{**ROW, "Checks": [{"Status": "critical"}]}],
        [{**ROW, "Checks": [None]}],
        [{**ROW, "Service": {**ROW["Service"], "Port": True}}],
    ],
)
async def test_malformed_registry_is_error_not_empty_membership(body):
    source = ConsulRegistry(
        "http://registry",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))),
    )
    try:
        with pytest.raises(ValueError):
            await source.read(POLICY)
    finally:
        await source.close()


@pytest.mark.parametrize("rows", [[], [{**ROW, "Checks": [{"Status": "passing", "Type": "serf"}]}]])
async def test_empty_and_node_only_health_do_not_admit_static_fallback(rows):
    source = ConsulRegistry(
        "http://registry",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows))),
    )
    try:
        assert await source.read({"service-b": ["stale:1234"]}) == POLICY
    finally:
        await source.close()


class FakeRegistry:
    def __init__(self):
        self.entered = asyncio.Event()
        self.error = False
        self.blocked = False
        self.closed = False

    async def read(self, policy):
        self.entered.set()
        if self.blocked:
            await asyncio.Event().wait()
        if self.error:
            raise httpx.ConnectError("registry unavailable")
        return {"service-b": ["127.0.0.1:50101"]}

    async def close(self):
        self.closed = True


async def test_catalog_lease_failure_and_expiry():
    registry = FakeRegistry()
    source = CatalogSource(registry, POLICY)
    assert (await source.read())["service-b"]
    registry.error = True
    with pytest.raises(httpx.ConnectError):
        await source.read()
    source.last_good = time.monotonic() - REGISTRY_LEASE - 1
    assert await source.read() == POLICY
    await source.close()
    assert registry.closed


async def test_catalog_read_timeout_still_publishes_expired_empty_membership(monkeypatch):
    from grpc_lb.controlplane.ads import SnapshotStore
    from grpc_lb.controlplane.application import SnapshotPublisher

    monkeypatch.setattr("grpc_lb.controlplane.sources.FETCH_TIMEOUT", 0.03)
    registry = FakeRegistry()
    source = CatalogSource(registry, POLICY)
    store = SnapshotStore()
    publisher = SnapshotPublisher(source, store)
    await publisher.publish()
    original = store.snapshot.version
    registry.blocked = True
    with pytest.raises(TimeoutError):
        await publisher.publish()
    assert store.snapshot.version == original
    source.last_good = time.monotonic() - REGISTRY_LEASE - 1
    assert await publisher.publish()
    assert store.snapshot.version != original
    await source.close()


@asynccontextmanager
async def active_health():
    server = grpc.aio.server()
    service = health.aio.HealthServicer()
    await service.set(health_name("service-b"), health_pb2.HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    registry = FakeRegistry()
    registry.blocked = True
    controller = HealthController(registry, POLICY, interval=0.02, timeout=0.05)
    controller.update({"service-b": [f"127.0.0.1:{port}"]})
    controller.start()
    try:
        yield controller, service, registry
    finally:
        await controller.close()
        await server.stop(0)
    assert not controller._tasks
    assert registry.closed


async def wait_count(controller, count):
    async with asyncio.timeout(3):
        while len((await controller.read())["service-b"]) != count:
            await asyncio.sleep(0.01)


async def test_health_changes_while_registry_is_blocked():
    async with active_health() as (controller, service, registry):
        await registry.entered.wait()
        await wait_count(controller, 1)
        await service.set(health_name("service-b"), health_pb2.HealthCheckResponse.NOT_SERVING)
        await wait_count(controller, 0)
        await service.set(health_name("service-b"), health_pb2.HealthCheckResponse.SERVING)
        await wait_count(controller, 1)


async def test_registry_lease_overrides_live_health_and_removal_cancels_probes():
    async with active_health() as (controller, _, _):
        await wait_count(controller, 1)
        controller.last_good = time.monotonic() - REGISTRY_LEASE - 1
        assert await controller.read() == POLICY
        controller.update(controller.members)
        await wait_count(controller, 1)
        controller.update({"service-b": []})
        assert await controller.read() == POLICY
        assert not controller.probes


async def test_open_transport_with_unresponsive_health_is_withdrawn():
    class BlockingHealth(health_pb2_grpc.HealthServicer):
        blocked = False

        async def Check(self, request, context):
            if self.blocked:
                await asyncio.Event().wait()
            return health_pb2.HealthCheckResponse(status=health_pb2.HealthCheckResponse.SERVING)

    server = grpc.aio.server()
    service = BlockingHealth()
    health_pb2_grpc.add_HealthServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    registry = FakeRegistry()
    controller = HealthController(registry, POLICY, interval=0.02, timeout=0.05)
    controller.update({"service-b": [f"127.0.0.1:{port}"]})
    try:
        await wait_count(controller, 1)
        service.blocked = True
        await wait_count(controller, 0)
    finally:
        await controller.close()
        await server.stop(0)
