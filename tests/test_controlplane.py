"""Migration regressions for xDS encoding, config boundaries and ownership."""

import asyncio
import json
import socket

import pytest
from envoy.config.endpoint.v3 import endpoint_pb2
from envoy.config.listener.v3 import listener_pb2
from envoy.extensions.filters.network.http_connection_manager.v3 import http_connection_manager_pb2

from grpc_lb.controlplane.ads import SnapshotStore
from grpc_lb.controlplane.application import ControlPlane, SnapshotPublisher
from grpc_lb.controlplane.config import EndpointResolver, RuntimeSettings, decode_config
from grpc_lb.controlplane.resources import CDS, EDS, LDS, ResourceBuilder


@pytest.mark.parametrize(
    "document",
    [
        "{}",
        '{"schema_version":2,"services":{"b":[]}}',
        '{"schema_version":1,"services":{"b":null}}',
        '{"schema_version":1,"services":{"b":[],"b":[]}}',
        '{"schema_version":1,"schema_version":1,"services":{"b":[]}}',
        '{"schema_version":1,"services":{"b":["localhost"]}}',
        '{"schema_version":1,"services":{"b":["127.0.0.1:0"]}}',
        '{"schema_version":1,"services":{"b":["0.0.0.0:1234"]}}',
        '{"schema_version":1,"services":{"b":["127.0.0.1:1234","127.0.0.1:1234"]}}',
        '{"schema_version":1,"services":{"xds:///b":[]}}',
        '{"schema_version":1,"services":{"b":[]},"unknown":true}',
        '{"schema_version":1,"services":{"b":[]}} {}',
        '{"schema_version":true,"services":{"b":[]}}',
    ],
)
async def test_invalid_discovery(document):
    with pytest.raises((ValueError, OSError)):
        await ResourceBuilder().build(decode_config(document))


async def test_proxyless_snapshot_and_stable_hash():
    members = {"service-b": ["127.0.0.1:50102", "127.0.0.1:50101"], "service-c": []}
    builder = ResourceBuilder()
    snapshot = await builder.build(members)
    listener = listener_pb2.Listener()
    assert snapshot.resources[LDS]["service-b"].Unpack(listener)
    manager = http_connection_manager_pb2.HttpConnectionManager()
    assert listener.api_listener.api_listener.Unpack(manager)
    assert manager.route_config.virtual_hosts[0].routes[0].route.cluster == "service-b"
    assert manager.http_filters[0].name == "envoy.filters.http.router"
    assert set(snapshot.resources) == {LDS, CDS, EDS}
    for name, count in [("service-b", 2), ("service-c", 0)]:
        assignment = endpoint_pb2.ClusterLoadAssignment()
        assert snapshot.resources[EDS][name].Unpack(assignment)
        assert len(assignment.endpoints[0].lb_endpoints) == count
    assert (await builder.build(dict(reversed(list(members.items()))))).version == snapshot.version
    members["service-b"].reverse()
    assert (await builder.build(members)).version == snapshot.version
    members["service-b"].pop()
    assert (await builder.build(members)).version != snapshot.version


async def test_dns_alias_duplicates_rejected(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1234))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    with pytest.raises(ValueError, match="duplicate"):
        await EndpointResolver().resolve({"b": ["alias:1234", "127.0.0.1:1234"]})


@pytest.mark.parametrize(
    "values",
    [
        {"XDS_LISTEN_ADDRESS": "missing-port"},
        {"XDS_LISTEN_ADDRESS": ":0"},
        {"CONSUL_ACTIVE_HEALTH": "yes"},
        {"CONSUL_ACTIVE_HEALTH": "1"},
        {"CONSUL_HTTP_ADDR": "ftp://registry"},
        {"CONSUL_HTTP_ADDR": "http://registry:99999"},
        {"CONSUL_HTTP_ADDR": "http://user:pass@registry"},
    ],
)
def test_invalid_runtime_settings(values):
    with pytest.raises(ValueError):
        RuntimeSettings.from_environment(values)


class MemorySource:
    def __init__(self, members=None, error=None):
        self.members = members or {"service-b": []}
        self.error = error
        self.closed = False
        self.changed = asyncio.Event()

    async def read(self):
        if self.error:
            raise self.error
        return self.members

    async def close(self):
        self.closed = True


async def test_source_closed_on_startup_failure():
    source = MemorySource(error=ValueError("source failed"))
    with pytest.raises(ValueError, match="source failed"):
        await ControlPlane("127.0.0.1:18000", source).run()
    assert source.closed


async def test_source_closed_on_busy_bind():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        reservation.listen()
        source = MemorySource()
        with pytest.raises(RuntimeError):
            await ControlPlane(f"127.0.0.1:{reservation.getsockname()[1]}", source).run()
        assert source.closed


async def test_normal_stop_cleans_source_and_releases_listener():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    source = MemorySource()
    app = ControlPlane(f"127.0.0.1:{port}", source)
    stop = asyncio.Event()
    stop.set()
    await app.run(stop)
    assert source.closed
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", port))
    with pytest.raises(RuntimeError, match="only run once"):
        await app.run(stop)


async def test_unexpected_background_failure_is_propagated_and_cleaned():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    source = MemorySource()
    original_read = source.read
    calls = 0

    async def fail_after_initial():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("background failed")
        return await original_read()

    source.read = fail_after_initial
    source.changed.set()
    app = ControlPlane(f"127.0.0.1:{port}", source)
    with pytest.raises(RuntimeError, match="background failed"):
        await asyncio.wait_for(app.run(), 3)
    assert source.closed


async def test_invalid_candidate_keeps_accepted_snapshot():
    source, store = MemorySource(), SnapshotStore()
    publisher = SnapshotPublisher(source, store)
    assert await publisher.publish()
    original = store.snapshot
    assert not await publisher.publish()
    source.members = {"service-b": ["0.0.0.0:1234"]}
    with pytest.raises(ValueError):
        await publisher.publish()
    assert store.snapshot is original


async def test_file_reload_rejects_bad_json(tmp_path):
    from grpc_lb.controlplane.sources import FileSource

    path = tmp_path / "discovery.json"
    path.write_text(json.dumps({"schema_version": 1, "services": {"b": []}}))
    source, store = FileSource(str(path)), SnapshotStore()
    publisher = SnapshotPublisher(source, store)
    await publisher.publish()
    original = store.snapshot
    path.write_text("invalid")
    with pytest.raises(ValueError):
        await publisher.publish()
    assert store.snapshot is original
