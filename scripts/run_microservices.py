"""Run a configured graph in separate local processes; --check then exits.

This launcher verifies the service layer directly and does not start Envoy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
from check_microservices import check, expectations
from grpc_health.v1 import health_pb2 as hp
from grpc_health.v1 import health_pb2_grpc as hr

from grpc_lb.microservices import health_name
from grpc_lb.topology import load_topology, split_endpoint, validate

ROOT = Path(__file__).resolve().parent.parent


def preflight(topology) -> None:
    """Refuse busy/nonlocal ports before starting ANY child or overwriting logs."""
    reservations = []
    try:
        for config in topology.values():
            for address in config.instances.values():
                host, port = split_endpoint(address)
                family = socket.AF_INET6 if ":" in host else socket.AF_INET
                listener = socket.socket(family, socket.SOCK_STREAM)
                reservations.append(listener)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    listener.bind((host, port))
                except OSError as exc:
                    raise RuntimeError(f"Local endpoint unavailable: {address}: {exc}") from exc
    finally:
        for listener in reservations:
            listener.close()


async def ready(address: str, service: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 15
    async with grpc.aio.insecure_channel(address) as channel:
        stub = hr.HealthStub(channel)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{service} exited; inspect results/microservices/*.log")
            try:
                response = await stub.Check(hp.HealthCheckRequest(service=health_name(service)), timeout=0.3)
                if response.status == hp.HealthCheckResponse.SERVING:
                    return
            except grpc.RpcError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"Service not ready: {address}")


async def run(path: Path, check_only: bool) -> None:
    entry, topology = load_topology(path)
    preflight(topology)
    processes, logs = [], []
    log_dir = ROOT / "results" / "microservices"
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        for service in validate(topology):
            for instance, address in topology[service].instances.items():
                env = dict(os.environ)
                env.update(
                    SERVICE_ID=service,
                    INSTANCE_ID=instance,
                    LISTEN_ADDRESS=address,
                    TOPOLOGY_PATH=str(path),
                    PYTHONPATH=str(ROOT / "src"),
                )
                # IDs are config data; use numeric filenames to avoid path traversal.
                log = (log_dir / f"process-{len(processes)}.log").open("w", encoding="utf-8")
                logs.append(log)
                process = subprocess.Popen(
                    [sys.executable, "-m", "grpc_lb.microservices"],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                processes.append(process)
                await ready(address, service, process)
                print(f"{service}/{instance}: {address} (PID {process.pid})", flush=True)
        if check_only:
            for instance, address in topology[entry].instances.items():
                expected = expectations(entry, topology)
                expected[entry] = {instance}
                requests = max(20, max(map(len, expected.values())) * 4)
                print(json.dumps(await check(address, entry, topology, expected, requests, 5)), flush=True)
        else:
            print("Ready. Ctrl+C stops all children. Envoy is not started.", flush=True)
            while True:
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("Service process exited; inspect results/microservices/*.log")
                await asyncio.sleep(0.5)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", default="configs/topology.local.json")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(Path(args.topology).resolve(), args.check))
    except KeyboardInterrupt:
        pass
    except (RuntimeError, TimeoutError, grpc.RpcError, ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"Service cluster FAILED: {exc}\n")


if __name__ == "__main__":
    main()
