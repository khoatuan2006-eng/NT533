"""Single-caller telemetry writer for explicitly enabled RL lab experiments."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .endpoints import EndpointResolver

# server: processing time the backend reports in a trailer (queue + work, no network).
# rpc:    time the caller waited for the RPC, network included; needed when the slowness
#         is on the path (the lab's slow-node3 delays packets, not processing).
LATENCY_SOURCES = ("server", "rpc")


class TelemetryCollector:
    def __init__(
        self,
        service: str,
        instances: dict[str, str],
        path: str,
        interval: float = 10,
        *,
        latency: str = "server",
    ):
        if latency not in LATENCY_SOURCES:
            raise ValueError(f"latency source must be one of: {', '.join(LATENCY_SOURCES)}")
        self.latency = latency
        self.service = service
        self.instances = dict(instances)
        self.path = Path(path)
        self.interval = interval
        self.samples: dict[str, list[float]] = {address: [] for address in instances.values()}
        self.failed: dict[str, int] = {address: 0 for address in instances.values()}
        # Time spent waiting in the server's admission queue, when the server reports it.
        self.queues: dict[str, list[float]] = {address: [] for address in instances.values()}
        self.errors = 0
        self.sequence = time.time_ns()
        self.task = None

    def observe(
        self, instance: str | None, duration_ms: float, *, ok: bool = True, queue_ms: float | None = None
    ):
        # A failure counts against an endpoint only when that server identified itself.
        if instance not in self.instances:
            self.errors += 1
        elif not ok:
            self.errors += 1
            self.failed[self.instances[instance]] += 1
        else:
            self.samples[self.instances[instance]].append(duration_ms)
            if queue_ms is not None:
                self.queues[self.instances[instance]].append(queue_ms)

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    async def flush(self):
        samples, failed, queues, errors = self.samples, self.failed, self.queues, self.errors
        self.samples = {address: [] for address in self.instances.values()}
        self.failed = {address: 0 for address in self.instances.values()}
        self.queues = {address: [] for address in self.instances.values()}
        self.errors = 0
        self.sequence += 1
        try:
            resolver = EndpointResolver()
            mapping, endpoint_errors, endpoint_queues = {}, {}, {}
            for address in samples:
                one = (await resolver.resolve({"target": [address]}))["target"]
                if len(one) != 1:
                    raise ValueError("instance must resolve to one endpoint")
                mapping[one[0]] = samples[address]
                endpoint_errors[one[0]] = failed[address]
                endpoint_queues[one[0]] = queues[address]
            if len(mapping) != len(samples):
                raise ValueError("ambiguous endpoint identity")
            document = {
                "timestamp": time.time(),
                "sequence": self.sequence,
                "service": self.service,
                "offered": sum(map(len, samples.values())) + errors,
                "errors": errors,
                "endpoints": mapping,
                "endpoint_errors": endpoint_errors,
                "endpoint_queue_ms": endpoint_queues,
            }
            temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            await asyncio.to_thread(temp.write_text, json.dumps(document), encoding="utf-8")
            await asyncio.to_thread(os.replace, temp, self.path)
        except (OSError, ValueError):
            # Controller expires the previous window and returns to baseline.
            pass

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
