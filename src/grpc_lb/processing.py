"""Processing RPC: admission, deadline budget, local work and dependency orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import quote

import grpc

from .dependencies import NoBackend, Pool
from .generated import processing_pb2 as pb
from .generated import processing_pb2_grpc as rpc
from .topology import ServiceConfig
from .workload import SimulatedWorkload, Workload, WorkloadUnavailable

LOG = logging.getLogger(__name__)


class Service(rpc.ProcessingServiceServicer):
    def __init__(
        self,
        config: ServiceConfig,
        instance_id: str,
        pools: dict[str, Pool],
        workload: Workload | None = None,
    ) -> None:
        self.config = config
        self.instance_id = instance_id
        self.pools = pools
        self.workload = workload if workload is not None else SimulatedWorkload(config)
        self.capacity = asyncio.Semaphore(config.concurrency)
        self.admitted = 0

    async def _validate_request(self, request: pb.TaskRequest, context: grpc.aio.ServicerContext) -> None:
        if not request.request_id or request.workload not in ("light", "heavy"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "request_id and light/heavy workload required"
            )
        if request.target_service != self.config.service_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Wrong target service")
        if self.config.service_id in request.visited_services:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Dependency cycle detected")
        if not 1 <= request.remaining_hops <= 64:
            await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Hop budget must be 1..64")

    async def _execute_dependencies(self, request: pb.TaskRequest, end: float) -> list[pb.TaskResult]:
        async def invoke(name: str) -> pb.TaskResult:
            child = pb.TaskRequest(
                request_id=request.request_id,
                target_service=name,
                workload=request.workload,
                visited_services=[*request.visited_services, self.config.service_id],
                remaining_hops=request.remaining_hops - 1,
            )
            return await self.pools[name].call(child, max(0, end - time.monotonic()))

        if self.config.mode == "parallel":
            tasks = [asyncio.create_task(invoke(name)) for name in self.pools]
            try:
                children = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        else:
            children = [await invoke(name) for name in self.pools]
        return children

    async def Execute(self, request: pb.TaskRequest, context: grpc.aio.ServicerContext) -> pb.TaskResult:
        start = time.perf_counter()
        status = "error"
        admitted = False
        remaining = context.time_remaining()
        budget = min(remaining, self.config.timeout_s) if remaining is not None else self.config.timeout_s
        end = time.monotonic() + budget
        queued_ms = 0.0
        budget_at_entry = budget
        # abort() sends trailers immediately, so identify this instance before any
        # failure path; callers use it to attribute errors to an endpoint.
        identity = ("x-instance-id", quote(self.instance_id, safe=""))
        context.set_trailing_metadata((identity,))
        try:
            await self._validate_request(request, context)
            if self.admitted >= self.config.concurrency * 2:
                status = "rejected"
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "Workload admission limit exceeded")
            # No await between check/increment: atomic within this event loop.
            self.admitted += 1
            admitted = True
            # One budget for queueing, local work and every downstream branch.
            async with asyncio.timeout(budget):
                async with self.capacity:
                    queued_ms = (time.perf_counter() - start) * 1000
                    await self.workload.execute(request)

                    children = await self._execute_dependencies(request, end)
                    result = pb.TaskResult(
                        request_id=request.request_id,
                        service_id=self.config.service_id,
                        instance_id=self.instance_id,
                        children=children,
                    )
            # asyncio timers are cooperative: an overdue callback may not have run
            # if the loop was stalled. Never return a local success after budget.
            if time.monotonic() >= end:
                raise TimeoutError("Processing budget exhausted before response")
            status = "ok"
            return result
        except TimeoutError:
            status = "timeout"
            await context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "Processing budget exhausted")
        except NoBackend as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except WorkloadUnavailable as exc:
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except grpc.aio.AioRpcError as exc:
            await context.abort(exc.code(), f"Dependency: {exc.details()}")
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if admitted:
                self.admitted -= 1
            duration_ms = (time.perf_counter() - start) * 1000
            # Custom diagnostic trailers; these are not a cross-host clock comparison.
            context.set_trailing_metadata(
                (
                    identity,
                    ("x-server-duration-ms", f"{duration_ms:.3f}"),
                    ("x-server-budget-ms", f"{budget_at_entry * 1000:.3f}"),
                    ("x-server-queue-ms", f"{queued_ms:.3f}"),
                )
            )
            LOG.info(
                json.dumps(
                    {
                        "service_id": self.config.service_id,
                        "instance_id": self.instance_id,
                        "request_id": request.request_id,
                        "workload": request.workload,
                        "status": status,
                        "duration_ms": duration_ms,
                        "queue_ms": queued_ms,
                        "budget_ms": budget_at_entry * 1000,
                    }
                )
            )
