"""Opt-in, single-process RL lab controller driven by external telemetry windows."""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path

from envoy.config.endpoint.v3 import endpoint_pb2

from grpc_lb.rl import QAgent, reward

from .resources import EDS
from .weighted import weighted_snapshot


class RLController:
    """One service, two endpoints. Invalid or stale data reverts to discovery snapshot."""

    def __init__(
        self, path: str, service: str, *, max_age: float = 15, seed: int = 42, model_path: str | None = None
    ):
        self.path, self.service, self.max_age = Path(path), service, max_age
        self.agent = QAgent(alpha=0.3, epsilon=0.1, seed=seed)
        self.sequence = -1
        self.members = ()
        self.weight = 5
        self.pending = None
        self.learning = True
        self.last_category = None
        self.category_count = 0
        if model_path:
            model = json.loads(Path(model_path).read_text(encoding="utf-8"))
            if model.get("kind") != "lookaside-q-v1":
                raise ValueError("unsupported RL model")
            for entry in model["q"]:
                state, values = entry["state"], entry["values"]
                if (
                    len(state) != 2
                    or any(type(x) is not int for x in state)
                    or len(values) != 3
                    or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)
                ):
                    raise ValueError("invalid RL model")
                self.agent.q[tuple(state)] = values
            self.learning = False

    def apply(self, snapshot):
        try:
            return self._apply(snapshot)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, OverflowError):
            self.pending = None
            self.members = ()
            self.weight = 5
            self.last_category = None
            self.category_count = 0
            return snapshot

    def _apply(self, snapshot):
        assignment = endpoint_pb2.ClusterLoadAssignment()
        if self.service not in snapshot.resources[EDS] or not snapshot.resources[EDS][self.service].Unpack(
            assignment
        ):
            raise ValueError("service unavailable")
        addresses = []
        for loc in assignment.endpoints:
            for endpoint in loc.lb_endpoints:
                addr = endpoint.endpoint.address.socket_address
                host = f"[{addr.address}]" if ":" in addr.address else addr.address
                addresses.append(f"{host}:{addr.port_value}")
        members = tuple(sorted(addresses))
        if len(members) != 2 or len(set(members)) != 2:
            raise ValueError("two distinct healthy endpoints required")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        now = time.time()
        timestamp = data["timestamp"]
        if (
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or not 0 <= now - timestamp <= self.max_age
        ):
            raise ValueError("stale telemetry")
        if data["service"] != self.service or type(data["sequence"]) is not int or data["sequence"] < 0:
            raise ValueError("invalid telemetry identity")
        metrics = data["endpoints"]
        if set(metrics) != set(members):
            raise ValueError("telemetry does not match membership")
        endpoint_errors = data.get("endpoint_errors", {})
        if (
            not isinstance(endpoint_errors, dict)
            or not set(endpoint_errors) <= set(members)
            or any(type(v) is not int or v < 0 for v in endpoint_errors.values())
            or type(data["errors"]) is not int
            or sum(endpoint_errors.values()) > data["errors"]
        ):
            raise ValueError("invalid endpoint error counts")
        medians = []
        for address in members:
            values = metrics[address]
            if (
                not isinstance(values, list)
                or len(values) < 3
                or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values)
            ):
                raise ValueError("invalid endpoint latency samples")
            # Effective latency = median / success rate, so an endpoint that
            # answers quickly but fails often does not look like the better one.
            failed = endpoint_errors.get(address, 0)
            medians.append(statistics.median(values) * (len(values) + failed) / len(values))
        if members != self.members:
            self.members, self.weight, self.pending, self.sequence = members, 5, None, -1
            self.last_category, self.category_count = None, 0
        if data["sequence"] < self.sequence:
            raise ValueError("out-of-order telemetry")
        if data["sequence"] > self.sequence:
            ratio = medians[0] / max(0.001, medians[1])
            state = (0 if ratio < 0.7 else 2 if ratio > 1.4 else 1, self.weight)
            if state[0] == self.last_category:
                self.category_count += 1
            else:
                self.last_category, self.category_count = state[0], 1
            offered, errors = data["offered"], data["errors"]
            samples = [v for address in members for v in metrics[address]]
            p95 = sorted(samples)[max(0, math.ceil(len(samples) * 0.95) - 1)]
            score = reward(offered, len(samples), errors, p95, 50, 0, mean_ms=statistics.mean(samples))
            if errors / offered > 0.1:
                raise ValueError("too many errors")
            # Keep enough traffic for observation and avoid starving an endpoint.
            allowed = tuple(a for a in (-1, 0, 1) if 3 <= self.weight + a <= 7)
            if self.pending and self.learning:
                old_state, old_action = self.pending
                self.agent.update(
                    old_state, old_action, score - 0.005 * abs(old_action), state, next_allowed=allowed
                )
            action = (
                self.agent.choose(state, explore=self.learning, allowed=allowed)
                if self.category_count >= 2
                else 0
            )
            self.weight += action
            self.pending = (state, action)
            self.sequence = data["sequence"]
        return weighted_snapshot(
            snapshot, self.service, dict(zip(members, (self.weight, 10 - self.weight), strict=True))
        )
