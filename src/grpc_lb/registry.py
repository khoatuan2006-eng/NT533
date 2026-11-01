"""Consul agent service registration; gRPC health checks run in the agent."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .service_identity import RPC_SERVICE, health_name

LOG = logging.getLogger(__name__)


class Registration:
    def __init__(self, agent: str, service: str, instance: str, host: str, port: int, token: str = ""):
        self.agent = agent.rstrip("/")
        self.instance = instance
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["X-Consul-Token"] = token
        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.payload = {
            "ID": instance,
            "Name": service,
            "Address": host,
            "Port": port,
            "Check": {
                "GRPC": f"{endpoint}/{health_name(service)}",
                "Interval": "1s",
                "Timeout": "500ms",
                "Status": "critical",
                "SuccessBeforePassing": 2,
                "FailuresBeforeCritical": 2,
                "DeregisterCriticalServiceAfter": "1m",
            },
        }

    def request(self, path: str, data=None):
        request = Request(
            self.agent + path,
            headers=self.headers,
            data=json.dumps(data).encode() if data is not None else None,
            method="PUT" if data is not None else "GET",
        )
        with urlopen(request, timeout=1) as response:
            return response.read()

    def ensure(self):
        try:
            current = json.loads(self.request("/v1/agent/service/" + quote(self.instance, safe="")))
            if not isinstance(current, dict):
                raise ValueError("Consul service response must be an object")
            if (
                all(current.get(key) == self.payload[key] for key in ("ID", "Address", "Port"))
                and current.get("Service") == self.payload["Name"]
            ):
                checks = json.loads(self.request("/v1/agent/checks"))
                if not isinstance(checks, dict) or any(
                    not isinstance(check, dict) for check in checks.values()
                ):
                    raise ValueError("Consul checks response must be an object of checks")
                if any(
                    check.get("ServiceID") == self.instance and check.get("Type") == "grpc"
                    for check in checks.values()
                ):
                    return
        except HTTPError as exc:
            if exc.code != 404:
                raise
        self.request("/v1/agent/service/register?replace-existing-checks=true", self.payload)
        LOG.info("registry_registered instance=%s service=%s", self.instance, self.payload["Name"])

    async def run(self):
        while True:
            try:
                # Cancelling to_thread does not stop the HTTP operation. Join
                # it before final deregistration so a late PUT cannot re-add a
                # service after shutdown has already removed it.
                operation = asyncio.create_task(asyncio.to_thread(self.ensure))
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError:
                    await asyncio.gather(operation, return_exceptions=True)
                    raise
            except (OSError, ValueError) as exc:
                LOG.warning("registry_registration_failed instance=%s error=%s", self.instance, exc)
            await asyncio.sleep(random.uniform(4, 6))

    async def close(self):
        try:
            await asyncio.to_thread(
                self.request, "/v1/agent/service/deregister/" + quote(self.instance, safe=""), {}
            )
        except OSError as exc:
            LOG.warning("registry_deregister_failed instance=%s error=%s", self.instance, exc)


async def watch_drain(runtime, path: str):
    """Deployment-owned drain marker drives the standard gRPC health service."""
    from grpc_health.v1 import health_pb2

    previous = None
    while True:
        draining = Path(path).exists()
        if draining != previous:
            state = (
                health_pb2.HealthCheckResponse.NOT_SERVING
                if draining
                else health_pb2.HealthCheckResponse.SERVING
            )
            for name in ("", RPC_SERVICE, health_name(runtime.handler.config.service_id)):
                await runtime.health.set(name, state)
            LOG.info("readiness_changed instance=%s draining=%s", runtime.handler.instance_id, draining)
            previous = draining
        await asyncio.sleep(0.2)
