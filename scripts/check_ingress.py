"""Check server-side balancing through ONE channel, without client-side LB.

Run after the ingress stack is healthy. Nonzero exit means validation failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter

import grpc

from grpc_lb.generated import workload_pb2, workload_pb2_grpc


async def check(address: str, expected: set[str], requests: int, timeout: float) -> dict:
    counts: Counter[str] = Counter()
    # One target, one persistent channel: only the proxy knows the replicas.
    async with grpc.aio.insecure_channel(address) as channel:
        await asyncio.wait_for(channel.channel_ready(), timeout=timeout)
        stub = workload_pb2_grpc.WorkloadServiceStub(channel)
        for index in range(requests):
            response = await stub.Execute(
                workload_pb2.ExecuteRequest(run_id="ingress-smoke", request_id=str(index)),
                timeout=timeout,
                metadata=(("x-request-id", f"ingress-smoke-{index}"),),
            )
            counts[response.backend_id] += 1
    if set(counts) != expected:
        raise RuntimeError(f"Expected backends {sorted(expected)}, got {dict(counts)}")
    # Sequential calls from one proxy should be nearly even under round robin.
    # Allow small variation for initial health/DNS transitions.
    if max(counts.values()) - min(counts.values()) > max(2, requests // 10):
        raise RuntimeError(f"Uneven round-robin distribution: {dict(counts)}")
    return {"address": address, "requests": requests, "backends": dict(counts)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:50050")
    parser.add_argument("--expected", default="replica-1,replica-2,replica-3")
    parser.add_argument("--requests", type=int, default=60)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()
    expected = {item.strip() for item in args.expected.split(",") if item.strip()}
    if not expected or args.requests < len(expected) or args.timeout <= 0:
        parser.error("Provide backends, at least one request per backend, and positive timeout")
    try:
        result = asyncio.run(check(args.address, expected, args.requests, args.timeout))
    except (grpc.RpcError, TimeoutError, RuntimeError) as exc:
        parser.exit(1, f"Ingress check FAILED: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
