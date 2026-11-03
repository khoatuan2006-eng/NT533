"""Minimal gRPC deadline experiment: no business graph, LB policy or per-RPC log.

The same RPC/server and clocks are used for synchronous and AsyncIO clients.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2 as hp

# Avoid importing application code or version-guarded generated gRPC stubs: this
# reproducer must also run an older isolated SDK after the app has been upgraded.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "grpc_lb" / "generated"))
import processing_pb2 as pb

METHOD = "/processing.v1.ProcessingService/Execute"


def execute_stub(channel):
    return channel.unary_unary(
        METHOD,
        request_serializer=pb.TaskRequest.SerializeToString,
        response_deserializer=pb.TaskResult.FromString,
        _registered_method=True,
    )


class Probe:
    def __init__(self):
        self.admitted = 0
        self.slots = asyncio.Semaphore(2)

    async def Execute(self, request, context):
        started = time.perf_counter_ns()
        budget = context.time_remaining()
        if request.workload == "burst":
            # Isolate rejection pressure from the original service graph/logging.
            if self.admitted >= 4:
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Probe admission full")
            self.admitted += 1
            try:
                async with self.slots:
                    await asyncio.sleep(0.05)
            finally:
                self.admitted -= 1
        else:
            await asyncio.sleep(0.05 if request.workload == "fast" else 0.2)
        duration = (time.perf_counter_ns() - started) / 1e6
        context.set_trailing_metadata(
            (
                ("x-probe-duration-ms", f"{duration:.3f}"),
                ("x-probe-budget-ms", f"{budget * 1000:.3f}" if budget is not None else "none"),
                ("x-probe-server-grpc", grpc.__version__),
            )
        )
        return pb.TaskResult(request_id=request.request_id, service_id="probe", instance_id="probe-1")


async def serve(address):
    server = grpc.aio.server(options=(("grpc.so_reuseport", 0),))
    methods = {
        "Execute": grpc.unary_unary_rpc_method_handler(
            Probe().Execute,
            request_deserializer=pb.TaskRequest.FromString,
            response_serializer=pb.TaskResult.SerializeToString,
        )
    }
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("processing.v1.ProcessingService", methods),)
    )
    server.add_registered_method_handlers("processing.v1.ProcessingService", methods)

    async def check_health(request, context):
        return hp.HealthCheckResponse(status=hp.HealthCheckResponse.SERVING)

    # Lab-only readiness endpoint, with the standard unary health wire schema.
    health_methods = {
        "Check": grpc.unary_unary_rpc_method_handler(
            check_health,
            request_deserializer=hp.HealthCheckRequest.FromString,
            response_serializer=hp.HealthCheckResponse.SerializeToString,
        )
    }
    server.add_generic_rpc_handlers(
        (grpc.method_handlers_generic_handler("grpc.health.v1.Health", health_methods),)
    )
    server.add_insecure_port(address)
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(0)


def clocks():
    return time.perf_counter_ns(), time.monotonic_ns(), time.time_ns()


def result_row(index, code, begin, created, received, remaining, metadata):
    return {
        "index": index,
        "code": code,
        "perf_ms": (received[0] - begin[0]) / 1e6,
        "monotonic_ms": (received[1] - begin[1]) / 1e6,
        "wall_ms": (received[2] - begin[2]) / 1e6,
        "call_creation_ms": (created[0] - begin[0]) / 1e6,
        "sdk_remaining_at_receive_s": remaining,
        "trailers": metadata,
    }


async def aio_client(args):
    rows, lags = [], []
    issued = 0

    async def observer():
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + 0.01
            await asyncio.sleep(0.01)
            lags.append(max(0, loop.time() - due) * 1000)

    observer_task = asyncio.create_task(observer())
    try:
        async with grpc.aio.insecure_channel(args.address) as channel:
            await asyncio.wait_for(channel.channel_ready(), 10)
            execute = execute_stub(channel)

            async def worker():
                nonlocal issued
                while issued < args.requests:
                    index = issued
                    issued += 1
                    request = pb.TaskRequest(
                        request_id=str(index), target_service="probe", workload=args.profile, remaining_hops=1
                    )
                    begin = clocks()
                    call = execute(request, timeout=args.timeout)
                    created = clocks()
                    try:
                        response = await call
                        received = clocks()
                        remaining = call.time_remaining()
                        if response.request_id != str(index):
                            raise RuntimeError("Response correlation failed")
                        metadata = dict(list(await call.trailing_metadata()))
                        code = "OK"
                    except grpc.aio.AioRpcError as exc:
                        received = clocks()
                        remaining = call.time_remaining()
                        metadata = dict(list(exc.trailing_metadata() or ()))
                        code = exc.code().name
                    rows.append(result_row(index, code, begin, created, received, remaining, metadata))

            await asyncio.gather(*(worker() for _ in range(args.concurrency)))
    finally:
        observer_task.cancel()
        await asyncio.gather(observer_task, return_exceptions=True)
    return rows, max(lags, default=0)


def sync_client(args):
    issued = 0
    lock = threading.Lock()
    with grpc.insecure_channel(args.address) as channel:
        grpc.channel_ready_future(channel).result(timeout=10)
        execute = execute_stub(channel)

        def worker():
            nonlocal issued
            rows = []
            while True:
                with lock:
                    if issued >= args.requests:
                        return rows
                    index = issued
                    issued += 1
                request = pb.TaskRequest(
                    request_id=str(index), target_service="probe", workload=args.profile, remaining_hops=1
                )
                begin = clocks()
                call = execute.future(request, timeout=args.timeout)
                created = clocks()
                try:
                    response = call.result()
                    received = clocks()
                    remaining = call.time_remaining()
                    if response.request_id != str(index):
                        raise RuntimeError("Response correlation failed")
                    code = "OK"
                except grpc.RpcError as exc:
                    received = clocks()
                    remaining = call.time_remaining()
                    code = exc.code().name
                metadata = dict(list(call.trailing_metadata() or ()))
                rows.append(result_row(index, code, begin, created, received, remaining, metadata))

        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [executor.submit(worker) for _ in range(args.concurrency)]
            return [row for future in futures for row in future.result()], None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--address", default="127.0.0.1:50251")
    parser.add_argument("--api", choices=["aio", "sync"], default="aio")
    parser.add_argument("--profile", choices=["fast", "slow", "burst"], default="fast")
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--requests", type=int, default=512)
    parser.add_argument("--concurrency", type=int, default=128)
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.requests <= 0 or args.concurrency <= 0:
        parser.error("Positive finite timeout, request count and concurrency required")
    if args.serve:
        asyncio.run(serve(args.address))
        return
    started = time.perf_counter()
    rows, lag = asyncio.run(aio_client(args)) if args.api == "aio" else sync_client(args)
    codes = Counter(row["code"] for row in rows)
    successful = [row for row in rows if row["code"] == "OK"]
    late = [row for row in successful if row["perf_ms"] > args.timeout * 1000]
    timings = sorted(row["perf_ms"] for row in rows)
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "grpc": grpc.__version__,
        "api": args.api,
        "profile": args.profile,
        "address": args.address,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "timeout_s": args.timeout,
        "codes": dict(codes),
        "late_ok": len(late),
        "elapsed_s": time.perf_counter() - started,
        "p99_all_ms": timings[math.ceil(len(timings) * 0.99) - 1],
        "max_loop_lag_ms": lag,
        "max_clock_disagreement_ms": max(abs(r["perf_ms"] - r["wall_ms"]) for r in rows),
        "max_creation_ms": max(r["call_creation_ms"] for r in rows),
        "rows": sorted(rows, key=lambda r: r["index"]),
    }
    print(json.dumps(report))


if __name__ == "__main__":
    main()
