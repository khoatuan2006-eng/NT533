"""Consistent membership, independent active health, and bounded stale leases."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import grpc
import httpx
from grpc_health.v1 import health_pb2, health_pb2_grpc

from grpc_lb.service_identity import health_name

from .config import (
    FETCH_TIMEOUT,
    REFRESH_INTERVAL,
    REGISTRY_LEASE,
    EndpointResolver,
    Membership,
    RuntimeSettings,
    decode_config,
    join_address,
)

LOG = logging.getLogger(__name__)


class SnapshotSource(Protocol):
    changed: asyncio.Event

    async def read(self) -> Membership: ...

    async def close(self) -> None: ...


class FileSource:
    def __init__(self, path: str):
        self.path = Path(path)
        self.changed = asyncio.Event()

    async def read(self) -> Membership:
        async with asyncio.timeout(FETCH_TIMEOUT):
            return decode_config(await asyncio.to_thread(self.path.read_bytes))

    async def close(self) -> None:
        pass


class MembershipReader(Protocol):
    async def read(self, policy: Membership) -> Membership: ...

    async def close(self) -> None: ...


class ConsulRegistry:
    def __init__(
        self, address: str, *, membership_only: bool = False, client: httpx.AsyncClient | None = None
    ):
        self.address = address.rstrip("/")
        self.membership_only = membership_only
        self.client = client or httpx.AsyncClient(timeout=2.0, trust_env=False)

    async def read(self, policy: Membership) -> Membership:
        members = {}
        for name in sorted(policy):
            query = "?consistent" if self.membership_only else "?passing=true&consistent"
            async with self.client.stream(
                "GET", f"{self.address}/v1/health/service/{quote(name, safe='')}{query}"
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > 4 << 20:
                        raise ValueError("registry response exceeds 4 MiB")
            rows = json.loads(body)
            if not isinstance(rows, list):
                raise ValueError("registry membership must be an array")
            addresses = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("invalid registry row")
                service, checks = row.get("Service"), row.get("Checks")
                if (
                    not isinstance(service, dict)
                    or service.get("Service") != name
                    or not isinstance(service.get("ID"), str)
                    or not service["ID"]
                    or type(service.get("Port")) is not int
                    or not 1 <= service["Port"] <= 65535
                    or not isinstance(checks, list)
                    or not checks
                    or any(not isinstance(check, dict) for check in checks)
                ):
                    raise ValueError(f"invalid registry service: {name}")
                if not self.membership_only and any(check.get("Status") != "passing" for check in checks):
                    raise ValueError("non-passing result in passing-only query")
                if not any(
                    check.get("ServiceID") == service["ID"] and check.get("Type") == "grpc"
                    for check in checks
                ):
                    continue
                node = row.get("Node") or {}
                if not isinstance(node, dict):
                    raise ValueError("invalid registry node")
                host = service.get("Address") or node.get("Address")
                if not isinstance(host, str) or not host:
                    raise ValueError("missing registry address")
                addresses.append(join_address(host, service["Port"]))
            members[name] = sorted(addresses)
        return members

    async def close(self) -> None:
        await self.client.aclose()


class CatalogSource:
    """Passing-only reference mode; registry failure never means empty membership."""

    def __init__(self, registry: MembershipReader, policy: Membership):
        self.registry, self.policy = registry, policy
        self.resolver = EndpointResolver()
        self.last_good: float | None = None
        self.changed = asyncio.Event()

    async def read(self) -> Membership:
        try:
            # Own this timeout here so expiry still produces an empty snapshot
            # when the complete multi-service registry read exceeds its budget.
            async with asyncio.timeout(FETCH_TIMEOUT):
                members = await self.resolver.resolve(await self.registry.read(self.policy))
            self.last_good = time.monotonic()
            return members
        except (httpx.HTTPError, OSError, ValueError, TimeoutError):
            if self.last_good is not None and time.monotonic() - self.last_good < REGISTRY_LEASE:
                raise
            LOG.warning("registry_lease_expired withdrawing_endpoints=true")
            return {name: [] for name in self.policy}

    async def close(self) -> None:
        await self.registry.close()


@dataclass(slots=True)
class ActiveProbe:
    task: asyncio.Task | None = None
    healthy: bool = False


class HealthController:
    """Registry RPCs cannot block health withdrawal or lease expiry publication."""

    def __init__(
        self, registry: MembershipReader, policy: Membership, *, interval=1.0, timeout=0.5, threshold=2
    ):
        self.registry, self.policy = registry, policy
        self.interval, self.timeout, self.threshold = interval, timeout, threshold
        self.members = {name: [] for name in policy}
        self.probes: dict[tuple[str, str], ActiveProbe] = {}
        self.last_good: float | None = None
        self.changed = asyncio.Event()
        self._registry_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._failure: BaseException | None = None
        self._expired = False

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self._failure = error
            self.changed.set()

    def start(self) -> None:
        if self._registry_task is None:
            self._registry_task = self._spawn(self._sync_registry())

    async def _sync_registry(self) -> None:
        resolver = EndpointResolver()
        while True:
            started = time.monotonic()
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    self.update(await resolver.resolve(await self.registry.read(self.policy)))
            except (httpx.HTTPError, OSError, ValueError, TimeoutError) as error:
                LOG.warning("registry_unavailable error=%s", error)
            await asyncio.sleep(max(0, REFRESH_INTERVAL - (time.monotonic() - started)))

    def update(self, members: Membership) -> None:
        desired = {(name, address) for name, addresses in members.items() for address in addresses}
        for key in self.probes.keys() - desired:
            probe = self.probes.pop(key)
            probe.task.cancel()
        for key in desired - self.probes.keys():
            probe = ActiveProbe()
            self.probes[key] = probe
            probe.task = self._spawn(self._probe(key, probe))
        self.members = members
        self.last_good = time.monotonic()
        self.changed.set()

    async def _probe(self, key: tuple[str, str], probe: ActiveProbe) -> None:
        service, address = key
        async with grpc.aio.insecure_channel(address) as channel:
            client = health_pb2_grpc.HealthStub(channel)
            successes = failures = 0
            while True:
                started = time.monotonic()
                try:
                    response = await client.Check(
                        health_pb2.HealthCheckRequest(service=health_name(service)), timeout=self.timeout
                    )
                    serving = response.status == health_pb2.HealthCheckResponse.SERVING
                except grpc.aio.AioRpcError:
                    serving = False
                successes, failures = (successes + 1, 0) if serving else (0, failures + 1)
                if self.probes.get(key) is probe:
                    healthy = probe.healthy
                    if successes >= self.threshold:
                        healthy = True
                    if failures >= self.threshold:
                        healthy = False
                    if healthy != probe.healthy:
                        probe.healthy = healthy
                        LOG.info("active_health service=%s address=%s serving=%s", service, address, healthy)
                        self.changed.set()
                await asyncio.sleep(max(0, self.interval - (time.monotonic() - started)))

    async def read(self) -> Membership:
        if self._failure is not None:
            raise RuntimeError("active discovery task failed") from self._failure
        expired = self.last_good is None or time.monotonic() - self.last_good >= REGISTRY_LEASE
        if expired != self._expired:
            LOG.warning("registry_lease_expired withdrawing_endpoints=%s", str(expired).lower())
            self._expired = expired
        return {
            name: [address for address in addresses if not expired and self.probes[(name, address)].healthy]
            for name, addresses in self.members.items()
        }

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self.probes.clear()
            await self.registry.close()


async def create_source(settings: RuntimeSettings) -> SnapshotSource:
    if not settings.consul_address:
        return FileSource(settings.discovery_path)
    policy = decode_config(await asyncio.to_thread(Path(settings.discovery_path).read_bytes))
    registry = ConsulRegistry(settings.consul_address, membership_only=settings.active_health)
    if not settings.active_health:
        return CatalogSource(registry, policy)
    controller = HealthController(registry, policy)
    controller.start()
    return controller
