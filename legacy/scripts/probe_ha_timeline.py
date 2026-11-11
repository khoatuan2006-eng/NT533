"""Co-located observer: local health, consistent catalog and live application RPCs."""

import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from urllib.request import urlopen

import grpc

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc

STOP = Path("/tmp/ha-observer-stop")


async def main():
    STOP.unlink(missing_ok=True)
    started = time.time()
    rows = []

    def read(tag, url):
        before = time.time()
        monotonic_start = time.perf_counter()
        row = {"source": tag, "unix_s": before}
        try:
            with urlopen(url, timeout=0.6) as response:
                data = json.load(response)
                if tag == "local_checks":
                    row["b2_status"] = [v["Status"] for v in data.values() if v.get("ServiceID") == "b-2"]
                else:
                    row["members"] = sorted(v["Service"]["ID"] for v in data)
        except Exception as exc:
            row["error"] = type(exc).__name__
        row["elapsed_ms"] = round((time.perf_counter() - monotonic_start) * 1000, 3)
        row["wall_clock_delta_ms"] = round((time.time() - before) * 1000 - row["elapsed_ms"], 3)
        return row

    async def observe(tag, url):
        while True:
            rows.append(await asyncio.to_thread(read, tag, url))
            await asyncio.sleep(0.2)

    async def calls(index):
        async with grpc.aio.insecure_channel("ingress:50050") as channel:
            stub = rpc.ProcessingServiceStub(channel)
            while True:
                before = time.time()
                monotonic_start = time.perf_counter()
                row = {"source": "rpc", "unix_s": before}
                try:
                    result = await stub.Execute(
                        pb.TaskRequest(
                            request_id=f"ha-probe-{index}-{before}",
                            target_service="service-a",
                            workload="light",
                            remaining_hops=64,
                        ),
                        timeout=3,
                    )
                    row["code"] = "OK"
                    row["backend"] = next(
                        c.instance_id for c in result.children if c.service_id == "service-b"
                    )
                except grpc.aio.AioRpcError as exc:
                    row["code"] = exc.code().name
                row["elapsed_ms"] = round((time.perf_counter() - monotonic_start) * 1000, 3)
                row["wall_clock_delta_ms"] = round((time.time() - before) * 1000 - row["elapsed_ms"], 3)
                rows.append(row)
                await asyncio.sleep(0.05)

    tasks = [asyncio.create_task(observe("local_checks", "http://consul-agent:8500/v1/agent/checks"))]
    for node in ("consul-agent", "consul-1", "consul-2", "consul-3"):
        tasks.append(
            asyncio.create_task(
                observe(node, f"http://{node}:8500/v1/health/service/service-b?passing=true&consistent")
            )
        )
    tasks += [asyncio.create_task(calls(i)) for i in range(4)]
    print(json.dumps({"observer_ready": True}), flush=True)
    try:
        while not STOP.exists() and time.time() - started < 120:
            await asyncio.sleep(0.1)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    rows.sort(key=lambda r: r["unix_s"])
    rpcs = [r for r in rows if r["source"] == "rpc"]
    print(
        json.dumps(
            {
                "started_unix_s": started,
                "finished_unix_s": time.time(),
                "codes": dict(Counter(r["code"] for r in rpcs)),
                "late_ok": sum(r["code"] == "OK" and r["elapsed_ms"] > 3000 for r in rpcs),
                "observations": rows,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
