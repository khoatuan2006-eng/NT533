"""Reusable dependency transports. Native xDS owns endpoint selection in xDS mode."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
import time
from typing import Protocol
from urllib.parse import quote, unquote

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

from .balancer import create_balancer
from .generated import processing_pb2 as pb
from .generated import processing_pb2_grpc as rpc
from .rl_telemetry import TelemetryCollector
from .service_identity import health_name
from .topology import ServiceConfig

LOG = logging.getLogger(__name__)


class NoBackend(Exception):
    pass


class StaticDependencyPool:
    def __init__(
        self, service_id: str, addresses: list[str], interval: float = 1, policy: str = "round_robin"
    ) -> None:
        if not service_id or not addresses or len(set(addresses)) != len(addresses):
            raise ValueError("Service ID and unique endpoints required")
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Health interval must be finite and positive")
        self.service_id = service_id
        self.interval = interval
        self.channels = {a: grpc.aio.insecure_channel(a) for a in addresses}
        self.stubs = {a: rpc.ProcessingServiceStub(ch) for a, ch in self.channels.items()}
        self.balancer = create_balancer(policy, addresses)
        self.balancer.set_active_backends([])
        self._tasks: list[asyncio.Task] = []

    async def refresh(self) -> None:
        async def probe(address: str) -> None:
            try:
                response = await health_pb2_grpc.HealthStub(self.channels[address]).Check(
                    health_pb2.HealthCheckRequest(service=health_name(self.service_id)), timeout=0.3
                )
                healthy = response.status == health_pb2.HealthCheckResponse.SERVING
            except grpc.RpcError:
                healthy = False
            self.balancer.set_backend_health(address, healthy)

        await asyncio.gather(*(probe(a) for a in self.channels))

    async def start(self) -> None:
        await self.refresh()
        self._tasks = [asyncio.create_task(self._watch_endpoint(a)) for a in self.channels]

    async def _watch_endpoint(self, address: str) -> None:
        # gRPC's standard streaming health protocol: status changes are pushed,
        # rather than every caller polling every endpoint forever.
        delay = self.interval
        while True:
            call = health_pb2_grpc.HealthStub(self.channels[address]).Watch(
                health_pb2.HealthCheckRequest(service=health_name(self.service_id))
            )
            try:
                async for response in call:
                    self.balancer.set_backend_health(
                        address, response.status == health_pb2.HealthCheckResponse.SERVING
                    )
                    delay = self.interval
            except grpc.aio.AioRpcError as exc:
                self.balancer.set_backend_health(address, False)
                if exc.code() == grpc.StatusCode.UNIMPLEMENTED:
                    # This deployment requires the standard health contract.
                    LOG.error("Health Watch unsupported for %s at %s", self.service_id, address)
                    return
            finally:
                call.cancel()
            self.balancer.set_backend_health(address, False)
            await asyncio.sleep(min(30, delay * random.uniform(0.8, 1.2)))
            delay = min(30, delay * 2)

    async def call(self, request: pb.TaskRequest, timeout: float) -> pb.TaskResult:
        address = await self.balancer.pick()
        if address is None:
            raise NoBackend(f"No healthy instance for {self.service_id}")
        # Application UNAVAILABLE can mean load shedding, not endpoint death.
        # Only the service-specific health watcher changes endpoint eligibility.
        try:
            return await self.stubs[address].Execute(
                request, timeout=timeout, metadata=(("x-request-id", quote(request.request_id, safe="")),)
            )
        finally:
            # Least Request counts in-flight RPCs; Round Robin ignores the release.
            self.balancer.release(address)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*(ch.close() for ch in self.channels.values()))


class XdsDependencyPool:
    """One reusable native xDS channel; endpoint selection belongs to gRPC Core."""

    def __init__(self, service_id: str, telemetry: TelemetryCollector | None = None) -> None:
        if not re.fullmatch(r"[a-z0-9-]{1,128}", service_id):
            raise ValueError("xDS service ID must match [a-z0-9-]{1,128}")
        if not os.environ.get("GRPC_XDS_BOOTSTRAP") and not os.environ.get("GRPC_XDS_BOOTSTRAP_CONFIG"):
            raise ValueError("xDS mode requires GRPC_XDS_BOOTSTRAP or GRPC_XDS_BOOTSTRAP_CONFIG")
        self.channel = grpc.aio.insecure_channel(f"xds:///{service_id}")
        self.stub = rpc.ProcessingServiceStub(self.channel)
        self.telemetry = telemetry

    async def start(self) -> None:
        # Discovery is asynchronous; each RPC retains its original deadline.
        if self.telemetry:
            self.telemetry.start()

    async def call(self, request: pb.TaskRequest, timeout: float) -> pb.TaskResult:
        start = time.monotonic()
        try:
            call = self.stub.Execute(
                request, timeout=timeout, metadata=(("x-request-id", quote(request.request_id, safe="")),)
            )
            result = await call
        except grpc.RpcError as error:
            if self.telemetry:
                # Only a server that answered can be blamed; transport failures stay unattributed.
                instance = None
                trailers = error.trailing_metadata() if isinstance(error, grpc.aio.AioRpcError) else None
                for key, value in trailers or ():
                    if key == "x-instance-id":
                        instance = unquote(value)
                        break
                self.telemetry.observe(instance, (time.monotonic() - start) * 1000, ok=False)
            raise
        if self.telemetry:
            duration = (time.monotonic() - start) * 1000
            if hasattr(call, "trailing_metadata"):
                metadata = await call.trailing_metadata()
                for key, value in metadata or ():
                    if key == "x-server-duration-ms":
                        try:
                            server_duration = float(value)
                            if math.isfinite(server_duration) and server_duration >= 0:
                                duration = server_duration
                        except ValueError:
                            pass
                        break
            self.telemetry.observe(result.instance_id, duration)
        return result

    async def close(self) -> None:
        if self.telemetry:
            await self.telemetry.close()
        await self.channel.close()


class DependencyClient(Protocol):
    """Dependency transport contract consumed by the processing handler."""

    async def start(self) -> None: ...
    async def call(self, request: pb.TaskRequest, timeout: float) -> pb.TaskResult: ...
    async def close(self) -> None: ...


Pool = DependencyClient


class DependencyPoolFactory:
    """Select a transport once at startup; request handling stays transport-neutral."""

    def __init__(
        self,
        mode: str = "static",
        health_interval: float = 1,
        *,
        telemetry_service: str = "",
        telemetry_path: str = "",
        static_policy: str = "round_robin",
    ) -> None:
        if mode not in ("static", "xds"):
            raise ValueError("DISCOVERY_MODE must be static or xds")
        create_balancer(static_policy)  # rejects unknown policy names at startup
        self.static_policy = static_policy
        if bool(telemetry_service) != bool(telemetry_path):
            raise ValueError("RL telemetry service and output path must be set together")
        self.mode = mode
        self.health_interval = health_interval
        self.telemetry_service = telemetry_service
        self.telemetry_path = telemetry_path

    def create(self, service_id: str, config: ServiceConfig) -> DependencyClient:
        if self.mode == "xds":
            telemetry = (
                TelemetryCollector(service_id, config.instances, self.telemetry_path)
                if self.telemetry_service == service_id
                else None
            )
            return XdsDependencyPool(service_id, telemetry)
        return StaticDependencyPool(
            service_id, list(config.instances.values()), self.health_interval, self.static_policy
        )


# Existing callers can keep the original static-pool name.
DependencyPool = StaticDependencyPool
