"""Server and channel ownership; usable by both the application and integration tests."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from .dependencies import DependencyPoolFactory, Pool
from .generated import processing_pb2_grpc as rpc
from .processing import Service
from .service_identity import RPC_SERVICE, health_name
from .topology import ServiceConfig, validate
from .workload import Workload


@dataclass
class RunningService:
    server: grpc.aio.Server
    health: health.aio.HealthServicer
    port: int
    pools: dict[str, Pool]
    handler: Service

    async def close(self) -> None:
        # Exit stacks attempt every cleanup even if an earlier one fails.
        async with AsyncExitStack() as cleanup:
            for pool in self.pools.values():
                cleanup.push_async_callback(pool.close)
            cleanup.push_async_callback(self.server.stop, 2)
            await self.health.enter_graceful_shutdown()


async def start_service(
    config: ServiceConfig,
    instance_id: str,
    topology: dict[str, ServiceConfig],
    address: str | None = None,
    *,
    health_interval: float = 1,
    discovery_mode: str = "static",
    workload: Workload | None = None,
    telemetry_service: str = "",
    telemetry_path: str = "",
    static_policy: str = "round_robin",
) -> RunningService:
    validate(topology)
    if topology.get(config.service_id) != config or instance_id not in config.instances:
        raise ValueError("Service and instance must be registered in topology")
    factory = DependencyPoolFactory(
        discovery_mode,
        health_interval,
        telemetry_service=telemetry_service,
        telemetry_path=telemetry_path,
        static_policy=static_policy,
    )
    pools: dict[str, Pool] = {}
    # Admission belongs to Execute so health probes remain available under load.
    # Refuse a duplicate listener rather than silently sharing the endpoint.
    server = grpc.aio.server(options=(("grpc.so_reuseport", 0),))
    checker = health.aio.HealthServicer()
    try:
        for dependency in config.dependencies:
            pools[dependency] = factory.create(dependency, topology[dependency])
        handler = Service(config, instance_id, pools, workload)
        rpc.add_ProcessingServiceServicer_to_server(handler, server)
        health_pb2_grpc.add_HealthServicer_to_server(checker, server)
        port = server.add_insecure_port(address or config.instances[instance_id])
        if not port:
            raise RuntimeError("Cannot bind service address")
        for pool in pools.values():
            await pool.start()
        for name in ("", RPC_SERVICE, health_name(config.service_id)):
            await checker.set(name, health_pb2.HealthCheckResponse.SERVING)
        await server.start()
        return RunningService(server, checker, port, pools, handler)
    except BaseException:
        async with AsyncExitStack() as cleanup:
            for pool in pools.values():
                cleanup.push_async_callback(pool.close)
            await server.stop(0)
        raise
