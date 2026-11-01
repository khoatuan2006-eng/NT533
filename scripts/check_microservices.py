"""Validate a configured processing graph through one endpoint/channel."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

import grpc

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc
from grpc_lb.topology import ServiceConfig, load_topology


def expectations(entry: str, topology: dict[str, ServiceConfig]) -> dict[str, set[str]]:
    expected = {}

    def visit(name):
        if name in expected:
            return
        expected[name] = set(topology[name].instances)
        for child in topology[name].dependencies:
            visit(child)

    visit(entry)
    return expected


async def check(
    address: str,
    entry: str,
    topology: dict[str, ServiceConfig],
    expected: dict[str, set[str]],
    requests: int,
    timeout: float,
    workload: str = "light",
) -> dict:
    counts = {service: Counter() for service in expected}

    def record(result, service, request_id):
        if result.request_id != request_id or result.service_id != service:
            raise RuntimeError("Request or service correlation mismatch")
        if result.instance_id not in expected[service]:
            raise RuntimeError(f"Unexpected instance: {result.instance_id}")
        counts[service][result.instance_id] += 1
        dependencies = topology[service].dependencies
        if [child.service_id for child in result.children] != list(dependencies):
            raise RuntimeError(f"Wrong child topology for {service}")
        for child in result.children:
            record(child, child.service_id, request_id)

    async with grpc.aio.insecure_channel(address) as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout)
        stub = rpc.ProcessingServiceStub(channel)
        for index in range(requests):
            request_id = f"workload-smoke-{index}"
            response = await stub.Execute(
                pb.TaskRequest(
                    request_id=request_id, target_service=entry, workload=workload, remaining_hops=64
                ),
                timeout=timeout,
                metadata=(("x-request-id", request_id),),
            )
            record(response, entry, request_id)
    for service, instances in expected.items():
        if set(counts[service]) != instances:
            raise RuntimeError(f"{service}: expected {sorted(instances)}, got {dict(counts[service])}")
    return {service: dict(values) for service, values in counts.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:50050")
    parser.add_argument("--topology", default="configs/topology.local.json")
    parser.add_argument("--expected", action="append", default=[], metavar="SERVICE=ID1,ID2")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--workload", choices=["light", "heavy"], default="light")
    args = parser.parse_args()
    try:
        entry, topology = load_topology(args.topology)
        expected = expectations(entry, topology)
        for override in args.expected:
            service, ids = override.split("=", 1)
            values = {s.strip() for s in ids.split(",") if s.strip()}
            if service not in expected or not values or not values <= expected[service]:
                raise ValueError("Expected instances must belong to a reachable service")
            expected[service] = values
        if args.requests < max(map(len, expected.values())) or args.timeout <= 0:
            raise ValueError("Sufficient requests and positive timeout required")
        result = asyncio.run(
            check(args.address, entry, topology, expected, args.requests, args.timeout, args.workload)
        )
    except (grpc.RpcError, TimeoutError, RuntimeError, ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Workload check FAILED: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
