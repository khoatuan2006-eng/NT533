"""State-of-the-world ADS v3 with independent named subscriptions per type."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import grpc
from envoy.service.discovery.v3 import ads_pb2_grpc, discovery_pb2

from .resources import CDS, LDS, Snapshot

LOG = logging.getLogger(__name__)


class SnapshotStore:
    """One atomic snapshot; slow streams coalesce updates to the latest version."""

    def __init__(self):
        self.snapshot: Snapshot | None = None
        self.watchers: set[asyncio.Event] = set()

    def publish(self, snapshot: Snapshot) -> bool:
        if self.snapshot is not None and self.snapshot.version == snapshot.version:
            return False
        self.snapshot = snapshot
        for watcher in self.watchers:
            watcher.set()
        LOG.info("snapshot_published version=%s", snapshot.version)
        return True


@dataclass(slots=True)
class Subscription:
    names: tuple[str, ...]
    wildcard: bool
    nonce: str = ""
    sent: tuple[str, tuple[str, ...], bool] | None = None


class AggregatedDiscoveryService(ads_pb2_grpc.AggregatedDiscoveryServiceServicer):
    def __init__(self, store: SnapshotStore):
        self.store = store

    async def StreamAggregatedResources(self, request_iterator, context):
        wake = asyncio.Event()
        requests = asyncio.Queue(maxsize=64)
        subscriptions: dict[str, Subscription] = {}
        reader_error = None
        ended = False
        sequence = 0

        async def receive():
            nonlocal reader_error, ended
            try:
                async for request in request_iterator:
                    await requests.put(request)
                    wake.set()
            except Exception as error:
                reader_error = error
            finally:
                ended = True
                wake.set()

        self.store.watchers.add(wake)
        reader = asyncio.create_task(receive())
        try:
            while True:
                await wake.wait()
                wake.clear()
                if reader_error is not None:
                    raise reader_error
                while not requests.empty():
                    request = requests.get_nowait()
                    snapshot = self.store.snapshot
                    if snapshot is None or request.type_url not in snapshot.resources:
                        await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "unsupported xDS resource type")
                    subscription = subscriptions.get(request.type_url)
                    if (
                        subscription is not None
                        and request.response_nonce
                        and request.response_nonce != subscription.nonce
                    ):
                        continue
                    if request.HasField("error_detail"):
                        LOG.warning(
                            "xds_nack type=%s error=%s", request.type_url, request.error_detail.message
                        )
                    elif request.response_nonce:
                        LOG.info("xds_ack type=%s version=%s", request.type_url, request.version_info)
                    names = tuple(sorted(set(request.resource_names)))
                    wildcard = not names and (
                        (subscription is None and request.type_url in {LDS, CDS})
                        or (subscription is not None and subscription.wildcard)
                    )
                    if subscription is None:
                        subscriptions[request.type_url] = Subscription(names, wildcard)
                    else:
                        subscription.names, subscription.wildcard = names, wildcard
                snapshot = self.store.snapshot
                if snapshot is not None:
                    for kind, subscription in subscriptions.items():
                        signature = (snapshot.version, subscription.names, subscription.wildcard)
                        if subscription.sent == signature:
                            continue  # ACK/NACK must not create a response loop.
                        subscription.sent = signature
                        if not subscription.names and not subscription.wildcard:
                            continue
                        entries = snapshot.resources[kind]
                        names = sorted(entries) if subscription.wildcard else subscription.names
                        sequence += 1
                        subscription.nonce = str(sequence)
                        yield discovery_pb2.DiscoveryResponse(
                            version_info=snapshot.version,
                            type_url=kind,
                            nonce=subscription.nonce,
                            resources=[entries[name] for name in names if name in entries],
                        )
                if ended:
                    return
        finally:
            self.store.watchers.discard(wake)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def DeltaAggregatedResources(self, request_iterator, context):
        await context.abort(grpc.StatusCode.UNIMPLEMENTED, "This control plane serves SotW ADS v3")
