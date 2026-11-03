"""Fresh native xDS caller used to verify control-plane HA without cached state."""

import asyncio
import json
from collections import Counter

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.microservices import XdsDependencyPool


async def main():
    pool = XdsDependencyPool("service-b")
    try:
        await asyncio.wait_for(pool.channel.channel_ready(), 15)
        counts = Counter()
        for index in range(40):
            result = await pool.call(
                pb.TaskRequest(
                    request_id=f"fresh-xds-{index}",
                    target_service="service-b",
                    workload="light",
                    remaining_hops=64,
                ),
                3,
            )
            assert result.service_id == "service-b"
            counts[result.instance_id] += 1
        print(json.dumps({"instances": dict(counts), "requests": 40}))
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
