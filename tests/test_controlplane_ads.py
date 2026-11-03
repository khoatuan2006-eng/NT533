"""Exercise the real ADS bidirectional gRPC transport, not a mock protocol."""

import asyncio
from contextlib import asynccontextmanager

import grpc
import pytest
from envoy.service.discovery.v3 import ads_pb2_grpc, discovery_pb2
from google.rpc.status_pb2 import Status

from grpc_lb.controlplane.ads import AggregatedDiscoveryService, SnapshotStore
from grpc_lb.controlplane.resources import CDS, EDS, LDS, ResourceBuilder


@asynccontextmanager
async def ads_stream():
    store = SnapshotStore()
    store.publish(await ResourceBuilder().build({"service-b": ["127.0.0.1:50101"], "service-c": []}))
    server = grpc.aio.server()
    ads_pb2_grpc.add_AggregatedDiscoveryServiceServicer_to_server(AggregatedDiscoveryService(store), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = ads_pb2_grpc.AggregatedDiscoveryServiceStub(channel).StreamAggregatedResources(timeout=5)
            try:
                yield call, store
            finally:
                call.cancel()
    finally:
        await server.stop(0)
    # Cancellation is delivered to the handler on the next event-loop turns.
    async with asyncio.timeout(1):
        while store.watchers:
            await asyncio.sleep(0.01)


async def receive(call):
    return await asyncio.wait_for(call.read(), 2)


async def test_lazy_named_subscriptions_do_not_wait_for_other_roots():
    async with ads_stream() as (call, _):
        for kind in (LDS, CDS, EDS):
            await call.write(discovery_pb2.DiscoveryRequest(type_url=kind, resource_names=["service-b"]))
            response = await receive(call)
            assert response.type_url == kind
            assert len(response.resources) == 1


@pytest.mark.parametrize("nack", [False, True])
async def test_ack_nack_no_response_loop_and_new_version_still_delivered(nack):
    async with ads_stream() as (call, store):
        await call.write(discovery_pb2.DiscoveryRequest(type_url=EDS, resource_names=["service-b"]))
        first = await receive(call)
        request = discovery_pb2.DiscoveryRequest(
            type_url=EDS,
            resource_names=["service-b"],
            response_nonce=first.nonce,
            version_info="previous" if nack else first.version_info,
        )
        if nack:
            request.error_detail.CopyFrom(Status(code=3, message="rejected test candidate"))
        await call.write(request)
        pending = asyncio.create_task(call.read())
        await asyncio.sleep(0.05)
        assert not pending.done()
        store.publish(await ResourceBuilder().build({"service-b": [], "service-c": []}))
        updated = await asyncio.wait_for(pending, 2)
        assert updated.version_info != first.version_info
        assert updated.nonce != first.nonce


async def test_stale_nonce_cannot_change_subscription():
    async with ads_stream() as (call, store):
        await call.write(discovery_pb2.DiscoveryRequest(type_url=LDS, resource_names=["service-b"]))
        first = await receive(call)
        await call.write(
            discovery_pb2.DiscoveryRequest(
                type_url=LDS,
                resource_names=["service-c"],
                response_nonce="stale",
            )
        )
        store.publish(await ResourceBuilder().build({"service-b": [], "service-c": []}))
        response = await receive(call)
        assert response.resources[0] == store.snapshot.resources[LDS]["service-b"]
        await call.write(
            discovery_pb2.DiscoveryRequest(
                type_url=LDS,
                resource_names=["service-b", "service-c"],
                response_nonce=response.nonce,
            )
        )
        assert len((await receive(call)).resources) == 2
        assert first.nonce != response.nonce


async def test_missing_resource_returns_empty():
    async with ads_stream() as (call, _):
        await call.write(discovery_pb2.DiscoveryRequest(type_url=LDS, resource_names=["absent"]))
        assert not (await receive(call)).resources


async def test_delta_is_explicitly_unsupported():
    server = grpc.aio.server()
    ads_pb2_grpc.add_AggregatedDiscoveryServiceServicer_to_server(
        AggregatedDiscoveryService(SnapshotStore()), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            call = ads_pb2_grpc.AggregatedDiscoveryServiceStub(channel).DeltaAggregatedResources(timeout=2)
            await call.write(discovery_pb2.DeltaDiscoveryRequest(type_url=LDS))
            with pytest.raises(grpc.aio.AioRpcError) as failure:
                await call.read()
            assert failure.value.code() == grpc.StatusCode.UNIMPLEMENTED
    finally:
        await server.stop(0)
