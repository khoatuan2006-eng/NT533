"""Network/process regression for the Windows runtime deadline failure.

This reproducer deliberately avoids the application graph and Envoy. It failed
with isolated grpcio 1.71.2 and must not report expired OK with the pinned runtime.
"""

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
import pytest
from grpc_health.v1 import health_pb2 as hp
from grpc_health.v1 import health_pb2_grpc as hr

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_no_expired_ok_under_rejection_pressure_in_separate_processes():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{reservation.getsockname()[1]}"
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "scripts" / "legacy" / "deadline_probe.py"),
        "--serve",
        "--address",
        address,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    client = None
    try:
        async with grpc.aio.insecure_channel(address) as channel:
            health = hr.HealthStub(channel)
            end = time.monotonic() + 10
            while True:
                try:
                    response = await health.Check(hp.HealthCheckRequest(service=""), timeout=0.3)
                    assert response.status == hp.HealthCheckResponse.SERVING
                    break
                except grpc.RpcError:
                    if time.monotonic() >= end:
                        raise
                    await asyncio.sleep(0.05)
        client = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "scripts" / "legacy" / "deadline_probe.py"),
            "--address",
            address,
            "--api",
            "aio",
            "--profile",
            "burst",
            "--requests",
            "1000",
            "--concurrency",
            "128",
            "--timeout",
            "3",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        stdout, stderr = await asyncio.wait_for(client.communicate(), 30)
        assert client.returncode == 0, stderr.decode(errors="replace")
        result = json.loads(stdout)
        assert sum(result["codes"].values()) == 1000
        assert result["codes"].get("OK", 0) > 0
        assert result["codes"].get("RESOURCE_EXHAUSTED", 0) > 0
        assert result["late_ok"] == 0, result
    finally:
        for process in (client, server):
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
