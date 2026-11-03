"""Deployment validation and real server cleanup at application boundaries."""

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from grpc_lb.application import ServiceApplication, ServiceSettings
from grpc_lb.service_runtime import RunningService
from grpc_lb.topology import ServiceConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "override",
    [
        {"SERVICE_ID": "missing"},
        {"INSTANCE_ID": "missing"},
        {"DISCOVERY_MODE": "unknown"},
        {"LISTEN_ADDRESS": "[::]:0"},
        {"SERVICE_ADVERTISE_ADDRESS": "0.0.0.0:50100"},
        {"CONSUL_HTTP_ADDR": "ftp://agent:8500"},
        {"CONSUL_HTTP_ADDR": "http://agent:99999"},
        {"DISCOVERY_MODE": "xds"},
    ],
)
def test_deployment_configuration_fails_before_startup(override):
    with pytest.raises(ValueError):
        ServiceSettings.from_environment(
            {"TOPOLOGY_PATH": str(ROOT / "configs/topology.local.json"), **override}
        )


def leaf_settings(**overrides):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{reservation.getsockname()[1]}"
    service = ServiceConfig("worker", {"worker-1": address})
    return ServiceSettings({"worker": service}, "worker", "worker-1", address, address, **overrides)


def assert_port_released(settings):
    host, port = settings.listen_address.split(":")
    with socket.socket() as listener:
        listener.bind((host, int(port)))


@pytest.mark.asyncio
async def test_registration_startup_failure_closes_already_started_server(monkeypatch):
    settings = leaf_settings(consul_address="http://127.0.0.1:8500")
    application = ServiceApplication(settings)

    def invalid_registration(*args):
        raise RuntimeError("registration setup failed")

    monkeypatch.setattr("grpc_lb.application.Registration", invalid_registration)
    with pytest.raises(RuntimeError, match="registration setup"):
        await application.start()
    assert application.runtime is None
    assert_port_released(settings)


@pytest.mark.asyncio
async def test_background_failure_is_surfaced_and_server_is_closed(monkeypatch):
    settings = leaf_settings(drain_file="unused-marker")
    application = ServiceApplication(settings)

    async def broken_readiness(*args):
        raise RuntimeError("readiness failed")

    monkeypatch.setattr("grpc_lb.application.watch_drain", broken_readiness)
    with pytest.raises(RuntimeError, match="readiness failed"):
        await asyncio.wait_for(application.run(asyncio.Event()), 3)
    assert application.runtime is None
    assert_port_released(settings)


@pytest.mark.asyncio
async def test_shutdown_attempts_all_resources_even_when_health_and_pool_fail():
    server = SimpleNamespace(stop=AsyncMock())
    health = SimpleNamespace(enter_graceful_shutdown=AsyncMock(side_effect=RuntimeError("health failed")))
    first = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("pool failed")))
    second = SimpleNamespace(close=AsyncMock())
    runtime = RunningService(server, health, 1, {"first": first, "second": second}, None)
    with pytest.raises(RuntimeError):
        await runtime.close()
    server.stop.assert_awaited_once()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
