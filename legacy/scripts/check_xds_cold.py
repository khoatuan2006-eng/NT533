"""Fresh process must not bypass unavailable discovery using static endpoints."""

import asyncio
import json
import time

import grpc

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.microservices import XdsDependencyPool


async def main():
    pool = XdsDependencyPool("service-b")
    start = time.monotonic()
    try:
        await pool.call(
            pb.TaskRequest(
                request_id="cold-xds", target_service="service-b", workload="light", remaining_hops=64
            ),
            2,
        )
        raise RuntimeError("Cold caller unexpectedly succeeded without discovery")
    except grpc.aio.AioRpcError as exc:
        elapsed = time.monotonic() - start
        if exc.code() not in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED) or elapsed > 3:
            raise
        print(json.dumps({"code": exc.code().name, "elapsed_s": elapsed, "static_fallback": False}))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
