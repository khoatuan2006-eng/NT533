"""Lifecycle regressions found during review of the original M1 layer."""

import asyncio
import socket

import pytest

from grpc_lb.client import WorkloadClient
from grpc_lb.server import _JsonlLogger, build_server


@pytest.mark.asyncio
async def test_duplicate_client_ids_fail_before_creating_channels(monkeypatch):
    def forbidden_channel(*args, **kwargs):
        raise AssertionError("Configuration must be validated before creating channels")

    monkeypatch.setattr("grpc.aio.insecure_channel", forbidden_channel)
    with pytest.raises(ValueError, match="unique"):
        WorkloadClient(
            [{"id": "same", "address": "localhost:50051"}, {"id": "same", "address": "localhost:50052"}]
        )


@pytest.mark.asyncio
async def test_client_close_waits_for_health_task_cleanup(monkeypatch):
    entered, cleaned = asyncio.Event(), asyncio.Event()
    client = WorkloadClient([{"id": "worker", "address": "localhost:50051"}])

    async def health_loop():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.03)
            cleaned.set()

    monkeypatch.setattr(client, "_health_check_loop", health_loop)
    client.start_health_check()
    await entered.wait()
    await client.close()
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_failed_workload_bind_closes_opened_log(tmp_path, monkeypatch):
    opened = []

    def recording_logger(path):
        logger = _JsonlLogger(path)
        opened.append(logger)
        return logger

    monkeypatch.setattr("grpc_lb.server._JsonlLogger", recording_logger)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen(1)
        port = reserved.getsockname()[1]
        with pytest.raises(RuntimeError):
            await build_server("worker", 0, 1, port, tmp_path / "failed.jsonl", f"127.0.0.1:{port}")
    assert len(opened) == 1
    assert opened[0]._fh.closed
