import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
import pytest
from grpc_health.v1 import health_pb2 as hp
from grpc_health.v1 import health_pb2_grpc as hr

from grpc_lb.microservices import health_name, start_service
from grpc_lb.registry import Registration, watch_drain
from grpc_lb.topology import ServiceConfig


@contextmanager
def agent():
    state = {"service": None, "registrations": 0, "check_present": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/v1/agent/checks":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {"service:b-1": {"ServiceID": "b-1", "Type": "grpc"}}
                        if state["check_present"]
                        else {}
                    ).encode()
                )
                return
            self.send_response(200 if state["service"] else 404)
            self.end_headers()
            self.wfile.write(json.dumps(state["service"]).encode())

        def do_PUT(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "/register?" in self.path:
                state["service"] = {**payload, "Service": payload["Name"]}
                state["registrations"] += 1
                state["check_present"] = True
            else:
                state["service"] = None
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_registration_recovers_without_resetting_existing_health():
    with agent() as (address, state):
        registration = Registration(address, "service-b", "b-1", "127.0.0.1", 50101)
        await asyncio.to_thread(registration.ensure)
        assert state["service"]["Check"]["GRPC"].endswith("/processing.v1.ProcessingService/service-b")
        await asyncio.to_thread(registration.ensure)
        assert state["registrations"] == 1
        state["check_present"] = False  # service survived, but its health check was lost
        await asyncio.to_thread(registration.ensure)
        assert state["registrations"] == 2
        assert state["check_present"]
        state["service"] = None  # agent lost its local registration
        await asyncio.to_thread(registration.ensure)
        assert state["registrations"] == 3
        await registration.close()
        assert state["service"] is None


@pytest.mark.asyncio
async def test_shutdown_does_not_allow_late_reregistration():
    with agent() as (address, state):
        registration = Registration(address, "service-b", "b-1", "127.0.0.1", 50101)
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        ensure = registration.ensure

        def slow_ensure():
            entered.set()
            release.wait(2)
            ensure()
            completed.set()

        registration.ensure = slow_ensure
        task = asyncio.create_task(registration.run())
        assert await asyncio.to_thread(entered.wait, 2)
        asyncio.get_running_loop().call_later(0.05, release.set)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registration.close()
        assert await asyncio.to_thread(completed.wait, 2)
        assert state["service"] is None


@pytest.mark.asyncio
async def test_drain_marker_updates_standard_grpc_health_without_stopping_transport(tmp_path):
    config = ServiceConfig("service-b", {"b-1": "127.0.0.1:50101"})
    runtime = await start_service(config, "b-1", {config.service_id: config}, "127.0.0.1:0")
    marker = tmp_path / "drain"
    task = asyncio.create_task(watch_drain(runtime, str(marker)))
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.port}") as channel:
            stub = hr.HealthStub(channel)

            async def expect(state):
                async with asyncio.timeout(3):
                    while (
                        await stub.Check(
                            hp.HealthCheckRequest(service=health_name(config.service_id)), timeout=1
                        )
                    ).status != state:
                        await asyncio.sleep(0.02)

            await expect(hp.HealthCheckResponse.SERVING)
            marker.touch()
            await expect(hp.HealthCheckResponse.NOT_SERVING)
            marker.unlink()
            await expect(hp.HealthCheckResponse.SERVING)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.close()
