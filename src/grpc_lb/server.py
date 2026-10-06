"""
server.py -- gRPC WorkloadService backend replica (Lau -- M1)

Cau hinh qua bien moi truong:
  BACKEND_ID       : ten replica (mac dinh "replica-1")
  SERVICE_DELAY_S  : do tre moi request, giay (mac dinh 0.1)
  SERVICE_DELAY_MS : do tre moi request, mili-giay (fallback neu SERVICE_DELAY_S khong duoc set)
  MAX_CONCURRENCY  : toi da request dong thoi (mac dinh 10)
  PORT             : cong lang nghe (mac dinh 50051)
  LOG_PATH         : duong dan file JSONL (mac dinh "/logs/server.jsonl")

JSONL schema (mot dong / request, ghi khi handler duoc kich hoat):
  {
    "schema_version": 1,
    "run_id":       str,
    "request_id":   str,
    "backend_id":   str,
    "queue_ms":     float,   # cho semaphore (monotonic), 0 neu bi huy truoc acquire
    "service_ms":   float,   # xu ly thuc su (0.0 neu bi huy khi dang cho)
    "status":       "ok" | "cancelled" | "error"
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable

import grpc
import grpc.aio
from grpc.health.v1 import health, health_pb2, health_pb2_grpc

from .generated import workload_pb2, workload_pb2_grpc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVICE_NAME = "workload.v1.WorkloadService"
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# JSONL logger
# ---------------------------------------------------------------------------

class _JsonlLogger:
    """Ghi log -- line-buffered, an toan voi GIL cho IO ngan."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # buffering=1 -> line-buffered
        self._fh = self._path.open("a", encoding="utf-8", buffering=1)

    def write(
        self,
        *,
        run_id: str,
        request_id: str,
        backend_id: str,
        queue_ms: float,
        service_ms: float,
        status: str,
    ) -> None:
        """Ghi dong bộ mot dong JSONL."""
        record = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": run_id,
            "request_id": request_id,
            "backend_id": backend_id,
            "queue_ms": round(queue_ms, 3),
            "service_ms": round(service_ms, 3),
            "status": status,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WorkloadServicer
# ---------------------------------------------------------------------------

class WorkloadServicer(workload_pb2_grpc.WorkloadServiceServicer):
    """
    Cung cap RPC Execute voi:
    - Gioi han dong thoi qua asyncio.Semaphore
    - Do queue_ms (cho semaphore) va service_ms (xu ly thuc su) bang monotonic
    - Xu ly deadline / cancel dung cach, khong ro ri suat semaphore
    - Ghi JSONL moi request da vao handler
    """

    def __init__(
        self,
        backend_id: str,
        delay_s: float,
        max_concurrency: int,
        jsonl_logger: _JsonlLogger,
        on_queued_cb: asyncio.Event | None = None,
    ) -> None:
        self._backend_id = backend_id
        self._delay_s = delay_s
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._logger = jsonl_logger
        # Hook tuy chon: set() khi handler vao giai doan cho semaphore.
        # Dung de dong bo trong kiem thu -- None khi khong can.
        self._on_queued_cb = on_queued_cb

    async def Execute(
        self,
        request: workload_pb2.ExecuteRequest,
        context: grpc.aio.ServicerContext,
    ) -> workload_pb2.ExecuteResponse:
        run_id = request.run_id
        request_id = request.request_id

        # Do queue_ms: tu khi handler nhan den khi acquire xong semaphore
        t_handler = time.monotonic()
        acquired = False

        # -- Pha 1: Cho semaphore -------------------------------------------
        # Tao task acquire rieng de co the shield khoi cancel cua Execute handler.
        acquire_task: asyncio.Task[bool] = asyncio.create_task(
            self._semaphore.acquire()
        )

        # Bao cho test biet handler da vao giai doan cho semaphore
        if self._on_queued_cb is not None:
            self._on_queued_cb.set()

        try:
            await asyncio.shield(acquire_task)
            acquired = True
        except asyncio.CancelledError:
            # Coroutine bi cancel khi dang cho semaphore (deadline / client cancel)
            if acquire_task.done() and not acquire_task.cancelled():
                # Task da hoan thanh truoc/ngay luc cancel -> da chiem 1 suat
                acquired = True
            else:
                # Task chua hoan thanh -> huy va cho don dep hoan toan
                acquire_task.cancel()
                try:
                    await acquire_task
                except (asyncio.CancelledError, Exception):
                    pass
                acquired = False

            queue_ms = (time.monotonic() - t_handler) * 1000.0
            self._logger.write(
                run_id=run_id,
                request_id=request_id,
                backend_id=self._backend_id,
                queue_ms=queue_ms,
                service_ms=0.0,
                status="cancelled",
            )
            if acquired:
                self._semaphore.release()
                acquired = False
            raise

        # -- Pha 2 & 3: Thuc thi co suat -- bao ve bang try-finally ----------
        # Tu day tro di, acquired luon la True va se duoc release trong finally.
        t_acquired = time.monotonic()
        queue_ms = (t_acquired - t_handler) * 1000.0
        t_start = 0.0

        try:
            # Pha 2: Kiem tra deadline truoc khi bat dau xu ly
            # (Deadline co the da het trong thoi gian cho semaphore)
            if context.cancelled():
                self._logger.write(
                    run_id=run_id,
                    request_id=request_id,
                    backend_id=self._backend_id,
                    queue_ms=queue_ms,
                    service_ms=0.0,
                    status="cancelled",
                )
                await context.abort(grpc.StatusCode.CANCELLED, "Deadline exceeded while queued")
                return workload_pb2.ExecuteResponse(backend_id=self._backend_id)

            # Pha 3: Thuc thi request
            t_start = time.monotonic()
            await self._do_work(context)
            service_ms = (time.monotonic() - t_start) * 1000.0
            self._logger.write(
                run_id=run_id,
                request_id=request_id,
                backend_id=self._backend_id,
                queue_ms=queue_ms,
                service_ms=service_ms,
                status="ok",
            )
            return workload_pb2.ExecuteResponse(backend_id=self._backend_id)

        except asyncio.CancelledError:
            service_ms = (time.monotonic() - t_start) * 1000.0 if t_start > 0 else 0.0
            self._logger.write(
                run_id=run_id,
                request_id=request_id,
                backend_id=self._backend_id,
                queue_ms=queue_ms,
                service_ms=service_ms,
                status="cancelled",
            )
            raise

        except Exception as exc:
            service_ms = (time.monotonic() - t_start) * 1000.0 if t_start > 0 else 0.0
            is_cancelled = context.cancelled() or "CANCELLED" in str(exc)
            status = "cancelled" if is_cancelled else "error"
            if status == "error":
                logger.error(
                    "Execute error [%s/%s]: %s", run_id, request_id, exc, exc_info=True
                )
            self._logger.write(
                run_id=run_id,
                request_id=request_id,
                backend_id=self._backend_id,
                queue_ms=queue_ms,
                service_ms=service_ms,
                status=status,
            )
            raise

        finally:
            if acquired:
                self._semaphore.release()
                acquired = False

    async def _do_work(self, context: grpc.aio.ServicerContext) -> None:
        """Mo phong xu ly voi polling de phat hien cancel / deadline som."""
        remaining = self._delay_s
        chunk = 0.05  # kiem tra moi 50 ms

        while remaining > 0:
            if context.cancelled():
                raise asyncio.CancelledError()
            sleep_time = min(chunk, remaining)
            await asyncio.sleep(sleep_time)
            remaining -= sleep_time

        # Kiem tra lan cuoi sau khi sleep xong
        if context.cancelled():
            raise asyncio.CancelledError()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Tra ve semaphore noi bo (dung trong kiem thu)."""
        return self._semaphore

    @property
    def semaphore_value(self) -> int:
        """So suat con trong -- max_concurrency sau khi toan bo request xong."""
        return self._semaphore._value  # type: ignore[attr-defined]  # noqa: SLF001


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------

async def build_server(
    backend_id: str,
    delay_s: float,
    max_concurrency: int,
    port: int,
    log_path: str | Path,
    listen_addr: str | None = None,
    on_queued_cb: asyncio.Event | None = None,
) -> tuple[grpc.aio.Server, _JsonlLogger, health.aio.HealthServicer, int]:
    """Tao va cau hinh server gRPC; tra ve (server, logger, health_servicer, bound_port).

    Khi port=0, OS tu chon cong trong; bound_port la cong thuc duoc gan.
    Ham nay huu ich cho kiem thu -- khong start server ngay.
    """
    jsonl_logger = _JsonlLogger(log_path)

    servicer = WorkloadServicer(
        backend_id=backend_id,
        delay_s=delay_s,
        max_concurrency=max_concurrency,
        jsonl_logger=jsonl_logger,
        on_queued_cb=on_queued_cb,
    )

    health_servicer = health.aio.HealthServicer()

    server = grpc.aio.server()
    workload_pb2_grpc.add_WorkloadServiceServicer_to_server(servicer, server)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)

    addr = listen_addr if listen_addr is not None else f"[::]:{port}"
    bound_port = server.add_insecure_port(addr)

    # Dat SERVING cho ca "" va ten service cu the
    await health_servicer.set("", health_pb2.HealthCheckResponse.SERVING)
    await health_servicer.set(_SERVICE_NAME, health_pb2.HealthCheckResponse.SERVING)

    return server, jsonl_logger, health_servicer, bound_port


async def serve(
    backend_id: str,
    delay_s: float,
    max_concurrency: int,
    port: int,
    log_path: str | Path,
) -> None:
    """Khoi dong server va cho tin hieu dung (SIGINT / SIGTERM)."""
    server, jsonl_logger, health_servicer, _ = await build_server(
        backend_id=backend_id,
        delay_s=delay_s,
        max_concurrency=max_concurrency,
        port=port,
        log_path=log_path,
    )

    await server.start()
    logger.info(
        "Server %s started on port %d | delay=%.3fs | max_concurrency=%d | log=%s",
        backend_id,
        port,
        delay_s,
        max_concurrency,
        log_path,
    )

    # Dung co kiem soat (graceful shutdown)
    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("Shutdown signal received for %s", backend_id)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            # Windows khong ho tro add_signal_handler cho tat ca signal
            pass

    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown interrupt received for %s", backend_id)
    finally:
        logger.info("Stopping server %s (grace=5s)...", backend_id)
        # Bao NOT_SERVING de client khong gui them request moi
        await health_servicer.set("", health_pb2.HealthCheckResponse.NOT_SERVING)
        await health_servicer.set(_SERVICE_NAME, health_pb2.HealthCheckResponse.NOT_SERVING)

        await server.stop(grace=5.0)
        jsonl_logger.close()
        logger.info("Server %s stopped.", backend_id)


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    backend_id = os.environ.get("BACKEND_ID", "replica-1")
    # Ho tro ca SERVICE_DELAY_S (giay) lan SERVICE_DELAY_MS (mili-giay)
    if "SERVICE_DELAY_S" in os.environ:
        delay_s = float(os.environ["SERVICE_DELAY_S"])
    elif "SERVICE_DELAY_MS" in os.environ:
        delay_s = float(os.environ["SERVICE_DELAY_MS"]) / 1000.0
    else:
        delay_s = 0.1

    max_concurrency = int(os.environ.get("MAX_CONCURRENCY", "10"))
    port = int(os.environ.get("PORT", "50051"))
    log_path = os.environ.get("LOG_PATH", "/logs/server.jsonl")

    try:
        asyncio.run(
            serve(
                backend_id=backend_id,
                delay_s=delay_s,
                max_concurrency=max_concurrency,
                port=port,
                log_path=log_path,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
