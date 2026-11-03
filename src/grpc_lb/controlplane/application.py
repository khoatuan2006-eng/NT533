"""Own the ADS listener, snapshot reconciliation, and discovery-source cleanup."""

from __future__ import annotations

import asyncio
import logging

import grpc
import httpx
from envoy.service.discovery.v3 import ads_pb2_grpc

from .ads import AggregatedDiscoveryService, SnapshotStore
from .config import FETCH_TIMEOUT, REFRESH_INTERVAL, join_address, split_address
from .resources import ResourceBuilder
from .sources import SnapshotSource

LOG = logging.getLogger(__name__)


class SnapshotPublisher:
    def __init__(self, source: SnapshotSource, store: SnapshotStore, policy=None):
        self.source, self.store = source, store
        self.builder = ResourceBuilder()
        self.policy = policy

    async def publish(self) -> bool:
        # Each source bounds its own I/O and applies its stale-lease policy.
        # Resource construction has a separate bound so a source's fail-closed
        # result is not discarded merely because its read exhausted the budget.
        members = await self.source.read()
        async with asyncio.timeout(FETCH_TIMEOUT):
            candidate = await self.builder.build(members)
        if self.policy is not None:
            candidate = self.policy.apply(candidate)
        return self.store.publish(candidate)


class ControlPlane:
    def __init__(self, address: str, source: SnapshotSource, policy=None):
        self.address, self.source = address, source
        self.store = SnapshotStore()
        self.publisher = SnapshotPublisher(source, self.store, policy)
        self.server = grpc.aio.server(options=(("grpc.so_reuseport", 0),))
        ads_pb2_grpc.add_AggregatedDiscoveryServiceServicer_to_server(
            AggregatedDiscoveryService(self.store), self.server
        )
        self._started = False

    async def _reconcile(self):
        while True:
            try:
                await asyncio.wait_for(self.source.changed.wait(), REFRESH_INTERVAL)
            except TimeoutError:
                pass
            self.source.changed.clear()
            try:
                await self.publisher.publish()
            except (OSError, ValueError, TimeoutError, httpx.HTTPError) as error:
                LOG.warning("snapshot_rejected error=%s", error)

    async def run(self, stop: asyncio.Event | None = None):
        if self._started:
            raise RuntimeError("control plane application can only run once")
        self._started = True
        tasks = []
        try:
            await self.publisher.publish()
            host, port = split_address(self.address, allow_empty_host=True)
            self.server.add_insecure_port(join_address(host or "::", port))
            await self.server.start()
            LOG.info("xds_listening address=%s", self.address)
            tasks = [
                asyncio.create_task(self._reconcile()),
                asyncio.create_task(self.server.wait_for_termination()),
                asyncio.create_task((stop or asyncio.Event()).wait()),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            try:
                # Cancelling wait_for_termination before stop cancels gRPC's
                # shared shutdown future. Stop the server while it is intact.
                await self.server.stop(0)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.source.close()
