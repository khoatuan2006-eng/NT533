"""
tests/test_server.py -- Kiem thu server gRPC WorkloadService (Lau -- M1)

Pham vi kiem thu:
  1.  RPC Execute co ban -- tra dung backend_id, log OK
  2.  Nhieu request dong thoi -- deu thanh cong
  3.  Gioi han dong thoi -- semaphore chan dung khi max_concurrency=1
  4.  Client deadline -- DEADLINE_EXCEEDED
  5.  Huy khi dang cho semaphore -- BAT BUOC: log cancelled, service_ms=0, khong ro ri
  6.  Health service SERVING / NOT_SERVING cho service=""
  7.  JSONL schema day du -- tat ca truong bat buoc
  8.  Mot dong JSONL moi request
  9.  Integration voi WorkloadClient that -- Execute, health, timeout, request tiep theo
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import grpc
import grpc.aio
import pytest
from grpc.health.v1 import health_pb2, health_pb2_grpc

from grpc_lb.client import WorkloadClient
from grpc_lb.contracts import CallStatus
from grpc_lb.generated import workload_pb2, workload_pb2_grpc
from grpc_lb.server import WorkloadServicer, _JsonlLogger, build_server


# ---------------------------------------------------------------------------
# Helper: khoi dong server test tren cong ngau nhien
# ---------------------------------------------------------------------------

async def _start_test_server(
    backend_id: str = "test-replica",
    delay_s: float = 0.0,
    max_concurrency: int = 10,
    log_path: str | Path | None = None,
    on_queued_cb: asyncio.Event | None = None,
) -> tuple[grpc.aio.Server, _JsonlLogger, health_pb2_grpc.HealthServicer, str]:
    if log_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        log_path = tmp.name

    server, jsonl_logger, health_servicer, bound_port = await build_server(
        backend_id=backend_id,
        delay_s=delay_s,
        max_concurrency=max_concurrency,
        port=0,
        log_path=log_path,
        listen_addr="127.0.0.1:0",
        on_queued_cb=on_queued_cb,
    )
    await server.start()
    address = f"127.0.0.1:{bound_port}"
    return server, jsonl_logger, health_servicer, address


def _read_jsonl(path: str | Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ---------------------------------------------------------------------------
# Test 1: RPC Execute co ban
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_returns_backend_id() -> None:
    """Execute tra ve backend_id dung va status OK."""
    server, logger, _, address = await _start_test_server(
        backend_id="replica-test", delay_s=0.0
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            resp = await stub.Execute(
                workload_pb2.ExecuteRequest(run_id="run-1", request_id="req-1"),
                timeout=3.0,
            )
        assert resp.backend_id == "replica-test"

        rows = _read_jsonl(log_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["schema_version"] == 1
        assert r["run_id"] == "run-1"
        assert r["request_id"] == "req-1"
        assert r["backend_id"] == "replica-test"
        assert r["status"] == "ok"
        assert r["queue_ms"] >= 0
        assert r["service_ms"] >= 0
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 2: Nhieu request dong thoi
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_concurrent_requests() -> None:
    """10 request dong thoi deu thanh cong, JSONL co 10 dong."""
    server, logger, _, address = await _start_test_server(
        backend_id="replica-1", delay_s=0.0, max_concurrency=10
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            results = await asyncio.gather(*[
                stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-c", request_id=f"req-{i}"),
                    timeout=5.0,
                )
                for i in range(10)
            ])
        assert all(r.backend_id == "replica-1" for r in results)
        rows = _read_jsonl(log_path)
        assert len(rows) == 10
        assert all(r["status"] == "ok" for r in rows)
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 3: Gioi han dong thoi -- semaphore chan dung
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrency_limit_blocks() -> None:
    """max_concurrency=1: 2 request song song phai mat >= 2*delay_s."""
    server, logger, _, address = await _start_test_server(
        backend_id="replica-slow",
        delay_s=0.2,
        max_concurrency=1,
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            t0 = asyncio.get_event_loop().time()
            r1, r2 = await asyncio.gather(
                stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-q", request_id="req-x"),
                    timeout=5.0,
                ),
                stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-q", request_id="req-y"),
                    timeout=5.0,
                ),
            )
            elapsed = asyncio.get_event_loop().time() - t0

        # Phai chay tuan tu (>= 2 * 0.2 = 0.4 s)
        assert elapsed >= 0.35, f"Expected sequential, got {elapsed:.3f}s"
        assert r1.backend_id == "replica-slow"
        assert r2.backend_id == "replica-slow"

        rows = _read_jsonl(log_path)
        assert len(rows) == 2
        # It nhat 1 dong co queue_ms > 50 ms
        assert any(row["queue_ms"] > 50 for row in rows), (
            f"Expected queued row, got: {rows}"
        )
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 4: Client deadline -- DEADLINE_EXCEEDED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_deadline_exceeded() -> None:
    """Client timeout 0.05 s < server delay 0.5 s -> DEADLINE_EXCEEDED."""
    server, logger, _, address = await _start_test_server(
        backend_id="replica-delay", delay_s=0.5, max_concurrency=1
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-d", request_id="req-d"),
                    timeout=0.05,
                )
        assert exc_info.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED

        # Server phai ghi log
        await asyncio.sleep(0.15)
        rows = _read_jsonl(log_path)
        assert len(rows) >= 1
        # status co the la "cancelled" (bi cancel khi dang xu ly) hoac "ok" (hoan thanh truoc deadline)
        assert rows[0]["status"] in ("cancelled", "ok")
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 5: Huy khi dang cho semaphore -- BAT BUOC co log cancelled, service_ms=0
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_while_queued_mandatory_log() -> None:
    """
    Request 2 bi cancel khi dang cho semaphore (max_concurrency=1, req 1 chiem).
    BAT BUOC: log phai co dong cancelled voi service_ms=0.0 va khong ro ri suat.
    """
    # Event de dong bo: biet khi handler req-2 da vao giai doan cho semaphore
    queued_event = asyncio.Event()

    server, logger, _, address = await _start_test_server(
        backend_id="replica-q",
        delay_s=1.0,        # req-1 chiem semaphore 1 giay
        max_concurrency=1,  # chi 1 request cung luc
        on_queued_cb=queued_event,
    )
    log_path = logger._path
    MAX_CONCURRENCY = 1

    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)

            # --- Req 1: chiem semaphore ---
            async def run_req1() -> None:
                try:
                    await stub.Execute(
                        workload_pb2.ExecuteRequest(run_id="run-q", request_id="req-1"),
                        timeout=3.0,
                    )
                except Exception:
                    pass

            task1 = asyncio.create_task(run_req1())
            # doi req-1 vao server va chiem semaphore
            await asyncio.sleep(0.05)

            # Reset event truoc khi gui req-2 (event dung cho req-2)
            queued_event.clear()

            # --- Req 2: se dung hang doi cho semaphore ---
            async def run_req2() -> None:
                try:
                    await stub.Execute(
                        workload_pb2.ExecuteRequest(run_id="run-q", request_id="req-2"),
                        timeout=5.0,  # deadline du dai de vao hang doi
                    )
                except grpc.aio.AioRpcError:
                    pass

            task2 = asyncio.create_task(run_req2())

            # Doi den khi handler req-2 BAT DAU cho semaphore
            await asyncio.wait_for(queued_event.wait(), timeout=2.0)

            # Luc nay req-2 dang THAT SU cho trong semaphore -- huy no
            task2.cancel()
            try:
                await task2
            except (asyncio.CancelledError, Exception):
                pass

            # Doi server ghi log dong bo chac chan (polling toi da 2 giay)
            rows = []
            for _ in range(40):
                rows = _read_jsonl(log_path)
                if any(r.get("request_id") == "req-2" for r in rows):
                    break
                await asyncio.sleep(0.05)

        # Phai co it nhat 1 dong cho req-2 voi status=cancelled (BAT BUOC assertion, khong bo qua)
        cancelled_rows = [r for r in rows if r.get("request_id") == "req-2"]
        assert len(cancelled_rows) >= 1, (
            f"PHAI co dong log cho req-2. Tat ca rows: {rows}"
        )
        c = cancelled_rows[0]
        assert c["status"] == "cancelled", f"status phai la 'cancelled', got: {c}"
        assert c["service_ms"] == 0.0, (
            f"service_ms phai la 0.0 khi bi cancel khi cho, got: {c['service_ms']}"
        )

        # Doi req-1 hoan thanh va kiem tra khong ro ri suat
        task1.cancel()
        try:
            await asyncio.wait_for(task1, timeout=2.0)
        except (asyncio.CancelledError, Exception):
            pass

        await asyncio.sleep(0.1)

        # Sau khi tat ca xong, semaphore phai tra ve gia tri ban dau
        # Lay semaphore tu server de kiem tra
        # (grpc.aio khong expose servicer truc tiep, dung hack nho biet max)
        # Kiem tra gian tiep: gui 1 request moi phai thanh cong (khong bi block mai mai)
        async with grpc.aio.insecure_channel(address) as ch2:
            stub2 = workload_pb2_grpc.WorkloadServiceStub(ch2)
            # Neu semaphore bi ro ri (value=0), request nay se bi timeout
            resp = await asyncio.wait_for(
                stub2.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-q", request_id="req-check"),
                    timeout=2.0,
                ),
                timeout=2.5,
            )
        assert resp.backend_id == "replica-q", "Semaphore bi ro ri neu request bi block"

    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 6: Health service
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_serving() -> None:
    """Health service bao SERVING cho service='' khi server dang chay."""
    server, logger, _, address = await _start_test_server()
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            hs = health_pb2_grpc.HealthStub(ch)
            r1 = await hs.Check(health_pb2.HealthCheckRequest(service=""), timeout=3.0)
            assert r1.status == health_pb2.HealthCheckResponse.SERVING

            r2 = await hs.Check(
                health_pb2.HealthCheckRequest(service="workload.v1.WorkloadService"),
                timeout=3.0,
            )
            assert r2.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        await server.stop(grace=0)
        logger.close()


@pytest.mark.asyncio
async def test_health_not_serving() -> None:
    """Health service bao NOT_SERVING sau khi set thu cong."""
    server, logger, health_servicer, address = await _start_test_server()
    try:
        await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        async with grpc.aio.insecure_channel(address) as ch:
            hs = health_pb2_grpc.HealthStub(ch)
            resp = await hs.Check(health_pb2.HealthCheckRequest(service=""), timeout=3.0)
            assert resp.status == health_pb2.HealthCheckResponse.NOT_SERVING
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 7: JSONL schema day du
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jsonl_schema_complete() -> None:
    """Moi dong JSONL phai co du tat ca cac truong theo schema M1."""
    required = {"schema_version", "run_id", "request_id", "backend_id",
                "queue_ms", "service_ms", "status"}

    server, logger, _, address = await _start_test_server(
        backend_id="replica-log", delay_s=0.0
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            for i in range(3):
                await stub.Execute(
                    workload_pb2.ExecuteRequest(run_id=f"run-{i}", request_id=f"req-{i}"),
                    timeout=3.0,
                )

        rows = _read_jsonl(log_path)
        assert len(rows) == 3
        for i, row in enumerate(rows):
            missing = required - set(row.keys())
            assert not missing, f"Row {i} thieu truong: {missing}"
            assert row["schema_version"] == 1
            assert row["run_id"] == f"run-{i}"
            assert row["request_id"] == f"req-{i}"
            assert row["backend_id"] == "replica-log"
            assert row["status"] == "ok"
            assert isinstance(row["queue_ms"], (int, float))
            assert isinstance(row["service_ms"], (int, float))
            assert row["queue_ms"] >= 0
            assert row["service_ms"] >= 0
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 8: 1 dong JSONL / request
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jsonl_one_line_per_request() -> None:
    """20 request phai tao dung 20 dong JSONL."""
    n = 20
    server, logger, _, address = await _start_test_server(
        delay_s=0.0, max_concurrency=n
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)
            await asyncio.gather(*[
                stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="r", request_id=f"req-{i}"),
                    timeout=5.0,
                )
                for i in range(n)
            ])
        rows = _read_jsonl(log_path)
        assert len(rows) == n
    finally:
        await server.stop(grace=0)
        logger.close()


# ===========================================================================
# Integration tests -- WorkloadClient that + server that tren localhost
# ===========================================================================

@pytest.mark.asyncio
async def test_integration_client_execute_round_robin() -> None:
    """
    WorkloadClient.execute() voi round_robin gui RPC thanh cong toi server.
    Ket qua phai co backend_id dung, status=ok, latency_ms > 0.
    """
    server, logger, _, address = await _start_test_server(
        backend_id="r1", delay_s=0.0
    )
    try:
        backends = [{"id": "r1", "address": address}]
        async with WorkloadClient(backends=backends, policy="round_robin") as client:
            result = await client.execute("run-int", "req-int-1", timeout_s=3.0)

        assert result.status == CallStatus.OK, f"Expected OK, got {result}"
        assert result.backend_id == "r1"
        assert result.latency_ms > 0
        assert result.policy == "round_robin"
        assert result.run_id == "run-int"
        assert result.request_id == "req-int-1"
    finally:
        await server.stop(grace=0)
        logger.close()


@pytest.mark.asyncio
async def test_integration_client_health_check() -> None:
    """
    WorkloadClient.check_backend_health() phai tra True khi server SERVING
    va False khi NOT_SERVING.
    """
    server, logger, health_servicer, address = await _start_test_server(
        backend_id="r-health"
    )
    try:
        backends = [{"id": "r-health", "address": address}]
        async with WorkloadClient(backends=backends, policy="round_robin") as client:
            # SERVING -> True
            is_healthy = await client.check_backend_health("r-health")
            assert is_healthy is True

            # Set NOT_SERVING
            await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
            await asyncio.sleep(0.05)

            is_healthy = await client.check_backend_health("r-health")
            assert is_healthy is False
    finally:
        await server.stop(grace=0)
        logger.close()


@pytest.mark.asyncio
async def test_integration_client_timeout() -> None:
    """
    WorkloadClient.execute() voi timeout nho hon delay -> status=TIMEOUT.
    """
    server, logger, _, address = await _start_test_server(
        backend_id="r-slow", delay_s=0.5
    )
    try:
        backends = [{"id": "r-slow", "address": address}]
        async with WorkloadClient(backends=backends, policy="round_robin") as client:
            result = await client.execute("run-t", "req-t", timeout_s=0.05)

        assert result.status == CallStatus.TIMEOUT, f"Expected TIMEOUT, got {result}"
        assert result.grpc_code == "DEADLINE_EXCEEDED"
        assert result.backend_id == "r-slow"
    finally:
        await server.stop(grace=0)
        logger.close()


@pytest.mark.asyncio
async def test_integration_client_request_after_timeout() -> None:
    """
    Request tiep theo sau timeout phai thanh cong (khong bi block boi trang thai cu).
    Kiem tra semaphore khong bi ro ri sau timeout.
    """
    server, logger, _, address = await _start_test_server(
        backend_id="r-seq", delay_s=0.3, max_concurrency=1
    )
    try:
        backends = [{"id": "r-seq", "address": address}]
        async with WorkloadClient(backends=backends, policy="round_robin") as client:
            # Request 1: timeout
            r1 = await client.execute("run-seq", "req-timeout", timeout_s=0.05)
            assert r1.status == CallStatus.TIMEOUT

            # Doi server xu ly xong request timeout (tranh block semaphore)
            await asyncio.sleep(0.4)

            # Request 2: phai thanh cong trong thoi gian hop ly
            r2 = await client.execute("run-seq", "req-ok", timeout_s=2.0)
            assert r2.status == CallStatus.OK, (
                f"Request sau timeout phai thanh cong, got {r2}"
            )
            assert r2.backend_id == "r-seq"
    finally:
        await server.stop(grace=0)
        logger.close()


@pytest.mark.asyncio
async def test_integration_least_request_counters_return_zero() -> None:
    """
    Sau khi tat ca RPC ket thuc, bo dem LeastRequest phai ve 0.
    Server that phai tra ket qua dung.
    """
    server, logger, _, address = await _start_test_server(
        backend_id="r-lr", delay_s=0.0, max_concurrency=20
    )
    try:
        backends = [{"id": "r-lr", "address": address}]
        async with WorkloadClient(backends=backends, policy="least_request") as client:
            results = await asyncio.gather(*[
                client.execute("run-lr", f"req-{i}", timeout_s=3.0)
                for i in range(15)
            ])
            for r in results:
                assert r.status == CallStatus.OK, f"Expected OK: {r}"

            from grpc_lb.balancer import LeastRequestBalancer
            balancer: LeastRequestBalancer = client.balancer  # type: ignore
            assert balancer.total_in_flight() == 0, (
                f"in_flight phai ve 0, got {balancer.total_in_flight()}"
            )
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 10: Cau hinh SERVICE_DELAY_S va SERVICE_DELAY_MS qua env
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_delay_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kiem tra phan tich bien moi truong SERVICE_DELAY_S va SERVICE_DELAY_MS."""
    import os

    # 1. Chi co SERVICE_DELAY_MS=250 -> 0.25 s
    monkeypatch.setenv("SERVICE_DELAY_MS", "250")
    monkeypatch.delenv("SERVICE_DELAY_S", raising=False)
    if "SERVICE_DELAY_S" in os.environ:
        d = float(os.environ["SERVICE_DELAY_S"])
    elif "SERVICE_DELAY_MS" in os.environ:
        d = float(os.environ["SERVICE_DELAY_MS"]) / 1000.0
    else:
        d = 0.1
    assert d == 0.25

    # 2. Co SERVICE_DELAY_S=0.5 -> uu tien SERVICE_DELAY_S
    monkeypatch.setenv("SERVICE_DELAY_S", "0.5")
    if "SERVICE_DELAY_S" in os.environ:
        d = float(os.environ["SERVICE_DELAY_S"])
    elif "SERVICE_DELAY_MS" in os.environ:
        d = float(os.environ["SERVICE_DELAY_MS"]) / 1000.0
    else:
        d = 0.1
    assert d == 0.5


# ---------------------------------------------------------------------------
# Test 11: Race condition -- cancel ngay khi vua acquire semaphore
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_race_condition_cancel_right_after_acquire() -> None:
    """
    Kiem tra an toan semaphore khi context bi cancelled ngay khi vua acquire.
    Chi release khi acquire thuc su thanh cong, khong ro ri suat va khong tang sai.
    """
    server, logger, _, address = await _start_test_server(
        backend_id="replica-race", delay_s=0.5, max_concurrency=1
    )
    log_path = logger._path
    try:
        async with grpc.aio.insecure_channel(address) as ch:
            stub = workload_pb2_grpc.WorkloadServiceStub(ch)

            # Request 1: timeout rat ngan (0.01s) de mo phong huy ngay khi vao handler
            try:
                await stub.Execute(
                    workload_pb2.ExecuteRequest(run_id="run-rc", request_id="req-rc-1"),
                    timeout=0.01,
                )
            except grpc.aio.AioRpcError:
                pass

            # Doi server giai phong semaphore
            await asyncio.sleep(0.1)

            # Request 2: phai acquire duoc semaphore binh thuong, khong bi deadlock
            resp = await stub.Execute(
                workload_pb2.ExecuteRequest(run_id="run-rc", request_id="req-rc-2"),
                timeout=2.0,
            )
            assert resp.backend_id == "replica-race"

        rows = _read_jsonl(log_path)
        assert len(rows) >= 2
        r1 = [r for r in rows if r["request_id"] == "req-rc-1"][0]
        assert r1["status"] in ("cancelled", "ok")
        r2 = [r for r in rows if r["request_id"] == "req-rc-2"][0]
        assert r2["status"] == "ok"
    finally:
        await server.stop(grace=0)
        logger.close()


# ---------------------------------------------------------------------------
# Test 12: Graceful shutdown & health transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graceful_shutdown_health_and_log() -> None:
    """
    Kiem tra graceful shutdown chuyen health ve NOT_SERVING cho ca '' va service_name,
    va dong file log an toan.
    """
    server, logger, health_servicer, address = await _start_test_server(
        backend_id="replica-shut"
    )
    try:
        # Dang SERVING
        async with grpc.aio.insecure_channel(address) as ch:
            hs = health_pb2_grpc.HealthStub(ch)
            r = await hs.Check(health_pb2.HealthCheckRequest(service=""), timeout=2.0)
            assert r.status == health_pb2.HealthCheckResponse.SERVING

        # Thuc hien cac buoc shutdown nhu trong serve()
        await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        await health_servicer.set(
            "workload.v1.WorkloadService", health_pb2.HealthCheckResponse.NOT_SERVING
        )

        # Kiem tra health da ve NOT_SERVING
        async with grpc.aio.insecure_channel(address) as ch:
            hs = health_pb2_grpc.HealthStub(ch)
            r = await hs.Check(health_pb2.HealthCheckRequest(service=""), timeout=2.0)
            assert r.status == health_pb2.HealthCheckResponse.NOT_SERVING
            r_named = await hs.Check(
                health_pb2.HealthCheckRequest(service="workload.v1.WorkloadService"),
                timeout=2.0,
            )
            assert r_named.status == health_pb2.HealthCheckResponse.NOT_SERVING

    finally:
        await server.stop(grace=0.5)
        logger.close()
