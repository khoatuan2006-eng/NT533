"""Validated deployment settings and ownership of the service process lifecycle."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .registry import Registration, watch_drain
from .service_runtime import RunningService, start_service
from .topology import ServiceConfig, load_topology, split_endpoint, validate

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServiceSettings:
    """Deployment configuration, fully checked before opening a socket."""

    topology: dict[str, ServiceConfig]
    service_id: str
    instance_id: str
    listen_address: str
    advertise_address: str
    discovery_mode: str = "static"
    consul_address: str = ""
    consul_token: str = field(default="", repr=False)
    drain_file: str = ""
    rl_telemetry_service: str = ""
    rl_telemetry_output: str = ""
    static_lb_policy: str = "round_robin"
    rl_telemetry_latency: str = "server"

    def __post_init__(self) -> None:
        validate(self.topology)
        if self.static_lb_policy not in ("round_robin", "least_request"):
            raise ValueError("STATIC_LB_POLICY must be round_robin or least_request")
        if self.rl_telemetry_latency not in ("server", "rpc"):
            raise ValueError("EXPERIMENTAL_RL_LATENCY must be server or rpc")
        if bool(self.rl_telemetry_service) != bool(self.rl_telemetry_output):
            raise ValueError(
                "EXPERIMENTAL_RL_SERVICE and EXPERIMENTAL_RL_TELEMETRY_OUTPUT must be set together"
            )
        if self.service_id not in self.topology:
            raise ValueError("Service is not in topology")
        if self.instance_id not in self.service.instances:
            raise ValueError("Instance is not registered for service")
        if self.discovery_mode not in ("static", "xds"):
            raise ValueError("DISCOVERY_MODE must be static or xds")
        _validate_listener(self.listen_address)
        split_endpoint(self.advertise_address)
        if self.consul_address:
            parsed = urlsplit(self.consul_address)
            if (
                self.consul_address != self.consul_address.strip()
                or parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.query
                or parsed.fragment
                or parsed.username
            ):
                raise ValueError("CONSUL_HTTP_ADDR must be an HTTP(S) agent URL")
            if parsed.port == 0:
                raise ValueError("CONSUL_HTTP_ADDR requires a nonzero port")

    @property
    def service(self) -> ServiceConfig:
        return self.topology[self.service_id]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> ServiceSettings:
        env = os.environ if environment is None else environment
        entry, topology = load_topology(env.get("TOPOLOGY_PATH", "configs/topology.local.json"))
        service_id = env.get("SERVICE_ID", entry)
        if service_id not in topology:
            raise ValueError(f"SERVICE_ID is not in topology: {service_id}")
        service = topology[service_id]
        instance_id = env.get("INSTANCE_ID", next(iter(service.instances)))
        if instance_id not in service.instances:
            raise ValueError(f"INSTANCE_ID is not registered for {service_id}: {instance_id}")
        mode = env.get("DISCOVERY_MODE", "static")
        if mode not in ("static", "xds"):
            raise ValueError("DISCOVERY_MODE must be static or xds")
        if mode == "xds" and service.dependencies:
            if not env.get("GRPC_XDS_BOOTSTRAP") and not env.get("GRPC_XDS_BOOTSTRAP_CONFIG"):
                raise ValueError("xDS mode requires GRPC_XDS_BOOTSTRAP or GRPC_XDS_BOOTSTRAP_CONFIG")
            if any(not re.fullmatch(r"[a-z0-9-]{1,128}", name) for name in service.dependencies):
                raise ValueError("xDS dependency IDs must match [a-z0-9-]{1,128}")
        listen = env.get("LISTEN_ADDRESS", service.instances[instance_id])
        advertise = env.get("SERVICE_ADVERTISE_ADDRESS", service.instances[instance_id])
        consul = env.get("CONSUL_HTTP_ADDR", "")
        return cls(
            topology,
            service_id,
            instance_id,
            listen,
            advertise,
            mode,
            consul,
            env.get("CONSUL_HTTP_TOKEN", ""),
            env.get("DRAIN_FILE", ""),
            env.get("EXPERIMENTAL_RL_SERVICE", ""),
            env.get("EXPERIMENTAL_RL_TELEMETRY_OUTPUT", ""),
            env.get("STATIC_LB_POLICY") or "round_robin",
            env.get("EXPERIMENTAL_RL_LATENCY") or "server",
        )


def _validate_listener(address: str) -> None:
    host, separator, port = address.rpartition(":")
    if (
        address != address.strip()
        or not separator
        or not host
        or not port.isascii()
        or not port.isdigit()
        or not 1 <= int(port) <= 65535
    ):
        raise ValueError("LISTEN_ADDRESS must contain a host and a port in 1..65535")
    if host.startswith("[") and host.endswith("]"):
        ipaddress.IPv6Address(host[1:-1])
    elif not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError("Invalid LISTEN_ADDRESS host")


class ServiceApplication:
    """Own the server, background tasks and registration in shutdown order."""

    def __init__(self, settings: ServiceSettings) -> None:
        self.settings = settings
        self.runtime: RunningService | None = None
        self.registration: Registration | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self.runtime is not None:
            raise RuntimeError("Service application is already started")
        settings = self.settings
        try:
            self.runtime = await start_service(
                settings.service,
                settings.instance_id,
                settings.topology,
                settings.listen_address,
                discovery_mode=settings.discovery_mode,
                telemetry_service=settings.rl_telemetry_service,
                telemetry_path=settings.rl_telemetry_output,
                telemetry_latency=settings.rl_telemetry_latency,
                static_policy=settings.static_lb_policy,
            )
            if settings.drain_file:
                self._tasks.append(asyncio.create_task(watch_drain(self.runtime, settings.drain_file)))
            if settings.consul_address:
                host, port = split_endpoint(settings.advertise_address)
                self.registration = Registration(
                    settings.consul_address,
                    settings.service_id,
                    settings.instance_id,
                    host,
                    port,
                    settings.consul_token,
                )
                self._tasks.append(asyncio.create_task(self.registration.run()))
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            if self.runtime is not None:
                await self.runtime.close()
        finally:
            self.runtime = None
            if self.registration is not None:
                await self.registration.close()
                self.registration = None

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop if stop is not None else asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        stop_task = None
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                    installed.append(sig)
                except (NotImplementedError, OSError):
                    pass
            await self.start()
            LOG.info("Listening on port %s discovery=%s", self.runtime.port, self.settings.discovery_mode)
            stop_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait([stop_task, *self._tasks], return_when=asyncio.FIRST_COMPLETED)
            if stop_task not in done:
                for task in done:
                    await task  # Surface unexpected background errors instead of silently losing readiness.
                raise RuntimeError("A service background task stopped unexpectedly")
        finally:
            if stop_task is not None:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
            try:
                await self.close()
            finally:
                for sig in installed:
                    loop.remove_signal_handler(sig)
