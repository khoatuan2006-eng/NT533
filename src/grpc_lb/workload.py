"""Replaceable local work executed within the request's admission and deadline."""

import asyncio
import random
from typing import Protocol

from .generated import processing_pb2 as pb
from .topology import ServiceConfig


class WorkloadUnavailable(Exception):
    """The local workload cannot serve this request."""


class Workload(Protocol):
    async def execute(self, request: pb.TaskRequest) -> None: ...


class SimulatedWorkload:
    def __init__(self, config: ServiceConfig) -> None:
        self.config = config

    async def execute(self, request: pb.TaskRequest) -> None:
        multiplier = self.config.heavy_multiplier if request.workload == "heavy" else 1
        await asyncio.sleep(self.config.delay_s * multiplier)
        if random.random() < self.config.failure_rate:
            raise WorkloadUnavailable("Simulated workload failure")
