"""Regression checks added during acceptance, beyond happy-path workflows."""

import asyncio
import json
import runpy
import socket
import time
from dataclasses import replace
from pathlib import Path

import grpc
import pytest
from grpc_health.v1 import health_pb2 as hp
from grpc_health.v1 import health_pb2_grpc as hr

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc
from grpc_lb.microservices import health_name, start_service
from grpc_lb.topology import ServiceConfig, load_topology, split_endpoint, validate


def task(service="leaf", request_id="acceptance"):
    return pb.TaskRequest(request_id=request_id, target_service=service, workload="light", remaining_hops=16)


@pytest.mark.asyncio
async def test_health_stays_available_while_workload_admission_is_full():
    config = ServiceConfig("leaf", {"leaf-1": "leaf:50051"}, concurrency=1, delay_s=5, timeout_s=10)
    node = await start_service(config, "leaf-1", {"leaf": config}, "127.0.0.1:0")
    calls = []
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{node.port}") as channel:
            stub = rpc.ProcessingServiceStub(channel)
            calls = [stub.Execute(task(request_id=str(i)), timeout=3) for i in range(2)]
            # Wait for the actual server admission counter rather than guessing queue timing.
            for _ in range(100):
                if getattr(node, "handler", None) and node.handler.admitted == 2:
                    break
                await asyncio.sleep(0.002)
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await stub.Execute(task(), timeout=1)
            assert exc.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
            state = await hr.HealthStub(channel).Check(
                hp.HealthCheckRequest(service=health_name("leaf")), timeout=1
            )
            assert state.status == hp.HealthCheckResponse.SERVING
            assert node.handler.admitted == 2
    finally:
        for call in calls:
            call.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        await node.close()


@pytest.mark.asyncio
async def test_unicode_request_id_survives_internal_metadata():
    leaf = ServiceConfig("leaf", {"leaf-1": "leaf:1"}, delay_s=0)
    root = ServiceConfig("root", {"root-1": "root:1"}, dependencies=("leaf",), delay_s=0)
    topology = {"root": root, "leaf": leaf}
    child = await start_service(leaf, "leaf-1", topology, "127.0.0.1:0")
    leaf.instances["leaf-1"] = f"127.0.0.1:{child.port}"
    parent = None
    try:
        parent = await start_service(root, "root-1", topology, "127.0.0.1:0")
        async with grpc.aio.insecure_channel(f"127.0.0.1:{parent.port}") as channel:
            response = await rpc.ProcessingServiceStub(channel).Execute(
                task("root", "yêu-cầu-你好"), timeout=2
            )
            assert response.request_id == response.children[0].request_id == "yêu-cầu-你好"
    finally:
        if parent:
            await parent.close()
        await child.close()


@pytest.mark.asyncio
async def test_instance_refuses_already_bound_port():
    config = ServiceConfig("leaf", {"leaf-1": "leaf:50051"}, delay_s=0)
    first = await start_service(config, "leaf-1", {"leaf": config}, "127.0.0.1:0")
    duplicate = None
    try:
        with pytest.raises(RuntimeError):
            duplicate = await start_service(config, "leaf-1", {"leaf": config}, f"127.0.0.1:{first.port}")
    finally:
        if duplicate:
            await duplicate.close()
        await first.close()


@pytest.mark.parametrize(
    "change",
    [
        {"delay_s": "slow"},
        {"delay_s": True},
        {"timeout_s": None},
        {"delay_s": 1e308, "heavy_multiplier": 1e308},
        {"dependencies": "leaf"},
        {"instances": ["leaf:123"]},
        {"instances": {"x": "http://localhost:123"}},
        {"instances": {"x": "localhost:0"}},
        {"instances": {"x": "localhost:65536"}},
        {"instances": {"x": "0.0.0.0:123"}},
    ],
)
def test_invalid_config_rejected_cleanly(change):
    config = replace(ServiceConfig("leaf", {"leaf-1": "leaf:1"}), **change)
    with pytest.raises(ValueError):
        validate({"leaf": config})


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"schema_version": 1},
        {"schema_version": True, "entry_service": "a", "services": {}},
        {"schema_version": 1, "entry_service": "a", "services": {"a": {"bogus": 1}}},
    ],
)
def test_invalid_json_schema_rejected(tmp_path, document):
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        load_topology(path)


def test_duplicate_json_keys_and_excessive_depth_rejected(tmp_path):
    path = tmp_path / "topology.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_topology(path)
    graph = {
        f"s{i}": ServiceConfig(
            f"s{i}", {f"i{i}": f"node:{i + 1}"}, dependencies=(f"s{i + 1}",) if i < 64 else ()
        )
        for i in range(65)
    }
    with pytest.raises(ValueError, match="64-hop"):
        validate(graph)
    assert split_endpoint("[::1]:123") == ("::1", 123)


def test_launcher_refuses_busy_port_before_starting_children(monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts/legacy").resolve()))
    preflight = runpy.run_path("scripts/legacy/run_microservices.py")["preflight"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address = f"127.0.0.1:{listener.getsockname()[1]}"
        with pytest.raises(RuntimeError, match="unavailable"):
            preflight({"leaf": ServiceConfig("leaf", {"leaf-1": address})})


@pytest.mark.asyncio
async def test_processing_budget_checked_even_if_timeout_callback_is_delayed(monkeypatch):
    # A stalled event loop can finish the handler before its overdue timer runs.
    def stalled_random():
        time.sleep(0.05)
        return 1.0

    monkeypatch.setattr("grpc_lb.workload.random.random", stalled_random)
    config = ServiceConfig("leaf", {"leaf-1": "leaf:1"}, delay_s=0, timeout_s=0.01)
    node = await start_service(config, "leaf-1", {"leaf": config}, "127.0.0.1:0")
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{node.port}") as channel:
            with pytest.raises(grpc.aio.AioRpcError) as exc:
                await rpc.ProcessingServiceStub(channel).Execute(task(), timeout=2)
            assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    finally:
        await node.close()
