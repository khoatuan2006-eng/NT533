"""Experimental real gRPC xDS locality-weight probe, isolated from deployments."""

import asyncio
import json
import os
from collections import Counter

import grpc
from envoy.service.discovery.v3 import ads_pb2_grpc

from grpc_lb.controlplane.ads import AggregatedDiscoveryService, SnapshotStore
from grpc_lb.controlplane.resources import ResourceBuilder
from grpc_lb.controlplane.weighted import weighted_snapshot
from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc
from grpc_lb.service_runtime import start_service
from grpc_lb.topology import ServiceConfig


async def main():
    config = ServiceConfig("worker", {"one": "one:50051", "two": "two:50051"}, delay_s=0)
    runtimes = []
    server = grpc.aio.server()
    channel = None
    try:
        for instance in config.instances:
            runtimes.append(await start_service(config, instance, {"worker": config}, "127.0.0.1:0"))
        addresses = [f"127.0.0.1:{runtime.port}" for runtime in runtimes]
        store = SnapshotStore()
        base = await ResourceBuilder().build({"worker": addresses})
        store.publish(weighted_snapshot(base, "worker", dict(zip(addresses, (8, 2), strict=True))))
        ads_pb2_grpc.add_AggregatedDiscoveryServiceServicer_to_server(
            AggregatedDiscoveryService(store), server
        )
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        os.environ["GRPC_XDS_BOOTSTRAP_CONFIG"] = json.dumps(
            {
                "xds_servers": [
                    {
                        "server_uri": f"127.0.0.1:{port}",
                        "channel_creds": [{"type": "insecure"}],
                        "server_features": ["xds_v3"],
                    }
                ],
                "node": {"id": "weighted-probe"},
            }
        )
        channel = grpc.aio.insecure_channel("xds:///worker")
        stub = rpc.ProcessingServiceStub(channel)
        request = pb.TaskRequest(
            request_id="weight-probe", target_service="worker", workload="light", remaining_hops=4
        )

        async def sample():
            counts = Counter()
            for _ in range(200):
                result = await stub.Execute(request, timeout=4, wait_for_ready=True)
                counts[result.instance_id] += 1
            return dict(counts)

        first = await sample()
        store.publish(weighted_snapshot(base, "worker", dict(zip(addresses, (2, 8), strict=True))))
        await asyncio.sleep(2)
        second = await sample()
        print(json.dumps({"weights_8_2": first, "weights_2_8": second}))
        if first.get("one", 0) < 125 or second.get("two", 0) < 125:
            raise RuntimeError("locality weighting did not change the observed distribution")
    finally:
        if channel:
            await channel.close()
        await server.stop(0)
        for runtime in runtimes:
            await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
