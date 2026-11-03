"""Configuration boundaries for the native xDS adapter (transport covered by Compose acceptance)."""

import pytest

from grpc_lb.microservices import XdsDependencyPool, start_service
from grpc_lb.topology import ServiceConfig


@pytest.mark.parametrize("name", ["", "service/b", "service b", "SERVICE-B", "b" * 129])
def test_invalid_xds_logical_name(name):
    with pytest.raises(ValueError, match="xDS service ID"):
        XdsDependencyPool(name)


@pytest.mark.asyncio
async def test_xds_requires_explicit_bootstrap(monkeypatch):
    monkeypatch.delenv("GRPC_XDS_BOOTSTRAP", raising=False)
    monkeypatch.delenv("GRPC_XDS_BOOTSTRAP_CONFIG", raising=False)
    with pytest.raises(ValueError, match="requires GRPC_XDS_BOOTSTRAP"):
        XdsDependencyPool("service-b")


@pytest.mark.asyncio
async def test_unknown_discovery_mode_fails_before_binding():
    config = ServiceConfig("worker", {"worker-1": "127.0.0.1:1"})
    with pytest.raises(ValueError, match="DISCOVERY_MODE"):
        await start_service(config, "worker-1", {"worker": config}, discovery_mode="typo")
