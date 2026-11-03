"""Single-caller telemetry writer for explicitly enabled RL lab experiments."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .controlplane.config import EndpointResolver


class TelemetryCollector:
    def __init__(self, service: str, instances: dict[str, str], path: str, interval: float = 10):
        self.service = service
        self.instances = dict(instances)
        self.path = Path(path)
        self.interval = interval
        self.samples: dict[str, list[float]] = {address: [] for address in instances.values()}
        self.errors = 0
        self.sequence = time.time_ns()
        self.task = None

    def observe(self, instance: str | None, duration_ms: float):
        if instance not in self.instances:
            self.errors += 1
        else:
            self.samples[self.instances[instance]].append(duration_ms)

    def start(self):
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    async def flush(self):
        samples, errors = self.samples, self.errors
        self.samples = {address: [] for address in self.instances.values()}
        self.errors = 0
        self.sequence += 1
        try:
            resolved = await EndpointResolver().resolve({"target": list(samples)})
            mapping = {}
            for address in samples:
                one = await EndpointResolver().resolve({"target": [address]})
                if len(one["target"]) != 1:
                    raise ValueError("instance must resolve to one endpoint")
                mapping[one["target"][0]] = samples[address]
            if len(mapping) != len(samples) or set(mapping) != set(resolved["target"]):
                raise ValueError("ambiguous endpoint identity")
            document = {
                "timestamp": time.time(),
                "sequence": self.sequence,
                "service": self.service,
                "offered": sum(map(len, samples.values())) + errors,
                "errors": errors,
                "endpoints": mapping,
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
