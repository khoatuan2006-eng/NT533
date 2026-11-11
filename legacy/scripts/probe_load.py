"""Run the same measured traffic inside Linux or another client environment."""

import argparse
import asyncio
import json

from acceptance_microservices import traffic

from grpc_lb.topology import load_topology


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="ingress:50050")
    parser.add_argument("--topology", default="/app/configs/topology.json")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=3)
    args = parser.parse_args()
    entry, topology = load_topology(args.topology)
    result = asyncio.run(
        traffic(args.address, entry, topology, args.requests, args.concurrency, "heavy", args.timeout)
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
