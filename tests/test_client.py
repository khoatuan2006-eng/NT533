from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

import grpc
from grpc.health.v1 import health, health_pb2, health_pb2_grpc

from grpc_lb.balancer import (
    LeastRequestBalancer,
    RoundRobinBalancer,
    create_balancer,
)
from grpc_lb.client import WorkloadClient
from grpc_lb.contracts import CallResult, CallStatus
from grpc_lb.generated import workload_pb2, workload_pb2_grpc


# ---------------------------------------------------------------------------
# Unit tests for Balancers
# ---------------------------------------------------------------------------

class TestRoundRobinBalancer:
    @pytest.mark.asyncio
    async def test_round_robin_cycling(self) -> None:
        backends = ["replica-1", "replica-2", "replica-3"]
        balancer = RoundRobinBalancer(backends)

        picks = [await balancer.pick() for _ in range(6)]
        assert picks == [
            "replica-1",
            "replica-2",
            "replica-3",
            "replica-1",
            "replica-2",
            "replica-3",
        ]

    @pytest.mark.asyncio
    async def test_round_robin_single_backend(self) -> None:
        balancer = RoundRobinBalancer(["replica-1"])
        picks = [await balancer.pick() for _ in range(3)]
        assert picks == ["replica-1", "replica-1", "replica-1"]

    @pytest.mark.asyncio
    async def test_round_robin_no_healthy_backend(self) -> None:
        balancer = RoundRobinBalancer(["replica-1", "replica-2"])
        balancer.set_backend_health("replica-1", False)
        balancer.set_backend_health("replica-2", False)

        assert await balancer.pick() is None

    @pytest.mark.asyncio
    async def test_round_robin_skips_unhealthy(self) -> None:
        backends = ["replica-1", "replica-2", "replica-3"]
        balancer = RoundRobinBalancer(backends)

        # Mark replica-2 as unhealthy
        balancer.set_backend_health("replica-2", False)
        picks = [await balancer.pick() for _ in range(4)]
        assert picks == ["replica-1", "replica-3", "replica-1", "replica-3"]

        # Restore replica-2
        balancer.set_backend_health("replica-2", True)
        picks_after = [await balancer.pick() for _ in range(3)]
        # Since last picked was replica-3, next should be replica-1 then replica-2
        assert "replica-2" in picks_after


class TestLeastRequestBalancer:
    @pytest.mark.asyncio
    async def test_least_request_tie_breaking(self) -> None:
        backends = ["replica-1", "replica-2", "replica-3"]
        balancer = LeastRequestBalancer(backends)

        # Initially all in-flight = 0. Picks should round-robin across all 3
        p1 = await balancer.pick()
        p2 = await balancer.pick()
        p3 = await balancer.pick()

        assert [p1, p2, p3] == ["replica-1", "replica-2", "replica-3"]
        assert balancer.get_in_flight("replica-1") == 1
        assert balancer.get_in_flight("replica-2") == 1
        assert balancer.get_in_flight("replica-3") == 1
        assert balancer.total_in_flight() == 3

    @pytest.mark.asyncio
    async def test_least_request_prefers_least_loaded(self) -> None:
        backends = ["replica-1", "replica-2", "replica-3"]
        balancer = LeastRequestBalancer(backends)

        # Pick 3 times -> in_flight for each is 1
        await balancer.pick()  # r1 -> 1
        await balancer.pick()  # r2 -> 1
        await balancer.pick()  # r3 -> 1

        # Complete r2's request -> r2 in_flight becomes 0
        await balancer.on_request_end("replica-2")
        assert balancer.get_in_flight("replica-2") == 0

        # Next pick must pick r2 because it has the minimum in-flight (0 vs 1, 1)
        next_pick = await balancer.pick()
        assert next_pick == "replica-2"
        assert balancer.get_in_flight("replica-2") == 1

    @pytest.mark.asyncio
    async def test_least_request_counter_returns_to_zero(self) -> None:
        backends = ["replica-1", "replica-2", "replica-3"]
        balancer = LeastRequestBalancer(backends)

        async def worker() -> None:
            chosen = await balancer.pick()
            assert chosen is not None
            # Simulate work
            await asyncio.sleep(0.01)
            await balancer.on_request_end(chosen)

        # Run 60 concurrent worker tasks
        await asyncio.gather(*(worker() for _ in range(60)))

        # Mandatory M1 condition: counters must return to 0
        assert balancer.total_in_flight() == 0
        for b in backends:
            assert balancer.get_in_flight(b) == 0

    @pytest.mark.asyncio
    async def test_least_request_skips_unhealthy(self) -> None:
        backends = ["replica-1", "replica-2"]
        balancer = LeastRequestBalancer(backends)

        # r1 is unhealthy with 0 in-flight; r2 is healthy with 5 in-flight
        balancer.set_backend_health("replica-1", False)
        for _ in range(5):
            await balancer.pick()

        # Next pick must still choose healthy r2, ignoring unhealthy r1
        assert await balancer.pick() == "replica-2"

    def test_factory_creation(self) -> None:
        rr = create_balancer("round_robin", ["r1"])
        assert isinstance(rr, RoundRobinBalancer)
        lr = create_balancer("least_request", ["r1"])
        assert isinstance(lr, LeastRequestBalancer)

        with pytest.raises(ValueError, match="Unknown load balancing policy"):
            create_balancer("unsupported_policy", ["r1"])


# ---------------------------------------------------------------------------
# Integration tests for WorkloadClient with live local gRPC servers
# ---------------------------------------------------------------------------

class MockWorkloadServicer(workload_pb2_grpc.WorkloadServiceServicer):
    def __init__(self, backend_id: str, delay_s: float = 0.0) -> None:
        self.backend_id = backend_id
        self.delay_s = delay_s
        self.call_count = 0

    async def Execute(
        self,
        request: workload_pb2.ExecuteRequest,
        context: grpc.aio.ServicerContext,
    ) -> workload_pb2.ExecuteResponse:
        self.call_count += 1
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return workload_pb2.ExecuteResponse(backend_id=self.backend_id)


class LocalTestCluster:
    """Helper to start and manage local gRPC test servers."""

    def __init__(self) -> None:
        self.servers: list[grpc.aio.Server] = []
        self.servicers: dict[str, MockWorkloadServicer] = {}
        self.health_servicers: dict[str, health.aio.HealthServicer] = {}
        self.backend_configs: list[dict[str, str]] = []

    async def add_server(
        self,
        backend_id: str,
        delay_s: float = 0.0,
        is_serving: bool = True,
    ) -> dict[str, str]:
        server = grpc.aio.server()
        servicer = MockWorkloadServicer(backend_id, delay_s)
        workload_pb2_grpc.add_WorkloadServiceServicer_to_server(servicer, server)

        health_servicer = health.aio.HealthServicer()
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if is_serving
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        await health_servicer.set("", status)
        await health_servicer.set("workload.v1.WorkloadService", status)
        health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()

        self.servers.append(server)
        self.servicers[backend_id] = servicer
        self.health_servicers[backend_id] = health_servicer

        cfg = {"id": backend_id, "address": f"127.0.0.1:{port}"}
        self.backend_configs.append(cfg)
        return cfg

    async def set_serving_status(self, backend_id: str, is_serving: bool) -> None:
        hs = self.health_servicers[backend_id]
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if is_serving
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        await hs.set("", status)
        await hs.set("workload.v1.WorkloadService", status)

    async def shutdown(self) -> None:
        for s in self.servers:
            await s.stop(grace=0.1)


@pytest.mark.asyncio
async def test_client_execution_round_robin() -> None:
    cluster = LocalTestCluster()
    try:
        await cluster.add_server("replica-1")
        await cluster.add_server("replica-2")
        await cluster.add_server("replica-3")

        async with WorkloadClient(
            backends=cluster.backend_configs,
            policy="round_robin",
        ) as client:
            res1 = await client.execute("run-1", "req-1", timeout_s=1.0)
            res2 = await client.execute("run-1", "req-2", timeout_s=1.0)
            res3 = await client.execute("run-1", "req-3", timeout_s=1.0)

            assert res1.status == CallStatus.OK
            assert res2.status == CallStatus.OK
            assert res3.status == CallStatus.OK

            assert [res1.backend_id, res2.backend_id, res3.backend_id] == [
                "replica-1",
                "replica-2",
                "replica-3",
            ]
            assert res1.latency_ms > 0
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_client_execution_least_request() -> None:
    cluster = LocalTestCluster()
    try:
        await cluster.add_server("replica-1", delay_s=0.05)
        await cluster.add_server("replica-2", delay_s=0.0)

        async with WorkloadClient(
            backends=cluster.backend_configs,
            policy="least_request",
        ) as client:
            # Send concurrent calls
            tasks = [
                client.execute("run-test", f"req-{i}", timeout_s=2.0)
                for i in range(10)
            ]
            results: list[CallResult] = await asyncio.gather(*tasks)

            for r in results:
                assert r.status == CallStatus.OK

            # LeastRequest counter must be back to 0
            lr_balancer: LeastRequestBalancer = client.balancer  # type: ignore
            assert lr_balancer.total_in_flight() == 0
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_client_timeout_handling() -> None:
    cluster = LocalTestCluster()
    try:
        # Server delay is 0.5s but timeout is 0.1s
        await cluster.add_server("replica-slow", delay_s=0.5)

        async with WorkloadClient(
            backends=cluster.backend_configs,
            policy="round_robin",
        ) as client:
            result = await client.execute("run-1", "req-timeout", timeout_s=0.1)

            assert result.status == CallStatus.TIMEOUT
            assert result.grpc_code == "DEADLINE_EXCEEDED"
            assert result.backend_id == "replica-slow"
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_client_no_backend_available() -> None:
    cluster = LocalTestCluster()
    try:
        cfg = await cluster.add_server("replica-1")
        async with WorkloadClient(
            backends=[cfg],
            policy="round_robin",
        ) as client:
            # Mark the only backend as unhealthy
            client.balancer.set_backend_health("replica-1", False)

            result = await client.execute("run-1", "req-none", timeout_s=1.0)
            assert result.status == CallStatus.NO_BACKEND
            assert result.backend_id is None
            assert result.error_message == "No healthy backend available"
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_client_health_check_recovery() -> None:
    cluster = LocalTestCluster()
    try:
        cfg1 = await cluster.add_server("replica-1", is_serving=True)
        cfg2 = await cluster.add_server("replica-2", is_serving=True)

        async with WorkloadClient(
            backends=[cfg1, cfg2],
            policy="round_robin",
            health_check_interval_s=0.05,
            health_check_timeout_s=0.1,
        ) as client:
            # Start background health checking
            client.start_health_check()
            await asyncio.sleep(0.1)
            assert client.balancer.get_active_backends() == ["replica-1", "replica-2"]

            # Simulate replica-2 failure
            await cluster.set_serving_status("replica-2", False)
            await asyncio.sleep(0.15)
            assert client.balancer.get_active_backends() == ["replica-1"]

            # Simulate replica-2 recovery
            await cluster.set_serving_status("replica-2", True)
            await asyncio.sleep(0.15)
            assert sorted(client.balancer.get_active_backends()) == ["replica-1", "replica-2"]
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_client_from_config_file() -> None:
    config_path = Path("configs/backends.json")
    client = WorkloadClient.from_config_file(config_path, policy="round_robin")
    assert client.policy == "round_robin"
    assert len(client._backend_configs) == 3
    await client.close()

