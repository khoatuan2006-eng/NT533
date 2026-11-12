"""Opt-in, single-process RL lab controller driven by external telemetry windows.

Two decision policies share everything else (telemetry checks, two-window hysteresis,
30-70% weight bounds, fallback to Round Robin), so comparing them isolates the decision:

  q     tabular Q-learning; learns online unless a frozen model is loaded
  rule  step towards the faster endpoint whenever the latency ratio leaves 0.7-1.4
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
from pathlib import Path

from envoy.config.endpoint.v3 import endpoint_pb2

from grpc_lb.rl import BETAS, BanditTable, QAgent, ShapeCore, reward

from .resources import EDS
from .weighted import weighted_snapshot

LOG = logging.getLogger(__name__)
POLICIES = ("q", "rule")


def rule_action(state, *, explore=False, allowed=(-1, 0, 1)) -> int:
    """Shift one step towards endpoint 0 when it is faster, away when slower, else hold."""
    action = 1 if state[0] == 0 else -1 if state[0] == 2 else 0
    return action if action in allowed else 0


def discovered_members(snapshot, service: str) -> tuple[str, ...]:
    """Sorted, distinct IP:port endpoints that discovery currently publishes for one service."""
    assignment = endpoint_pb2.ClusterLoadAssignment()
    if service not in snapshot.resources[EDS] or not snapshot.resources[EDS][service].Unpack(assignment):
        raise ValueError("service unavailable")
    addresses = []
    for loc in assignment.endpoints:
        for endpoint in loc.lb_endpoints:
            addr = endpoint.endpoint.address.socket_address
            host = f"[{addr.address}]" if ":" in addr.address else addr.address
            addresses.append(f"{host}:{addr.port_value}")
    if len(set(addresses)) != len(addresses):
        raise ValueError("duplicate endpoints")
    return tuple(sorted(addresses))


def read_window(path: Path, service: str, members, max_age: float) -> tuple[dict, list[float]]:
    """One validated telemetry window and the effective latency of each member, in order.

    Every controller goes through these checks, so they fall back to Round Robin on the same
    conditions: stale or foreign data, membership mismatch, bad error counts, < 3 samples.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    timestamp = data["timestamp"]
    if (
        type(timestamp) not in (int, float)
        or not math.isfinite(timestamp)
        or not 0 <= time.time() - timestamp <= max_age
    ):
        raise ValueError("stale telemetry")
    if data["service"] != service or type(data["sequence"]) is not int or data["sequence"] < 0:
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
    return data, medians


class RLController:
    """One service, two endpoints. Invalid or stale data reverts to discovery snapshot."""

    def __init__(
        self,
        path: str,
        service: str,
        *,
        max_age: float = 15,
        seed: int = 42,
        model_path: str | None = None,
        policy: str = "q",
    ):
        if policy not in POLICIES:
            raise ValueError(f"RL policy must be one of: {', '.join(POLICIES)}")
        if policy == "rule" and model_path:
            raise ValueError("the rule policy takes no model")
        self.path, self.service, self.max_age, self.policy = Path(path), service, max_age, policy
        self.fallback_reason = None
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
        if policy == "rule":
            self.agent.choose = rule_action
            self.learning = False

    def apply(self, snapshot):
        try:
            result = self._apply(snapshot)
            self.fallback_reason = None
            return result
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, OverflowError) as error:
            reason = f"{type(error).__name__}: {error}"
            if reason != self.fallback_reason:
                # Once per change of reason: apply() runs on every publication.
                LOG.info(json.dumps({"event": "rl_fallback", "policy": self.policy, "reason": reason}))
                self.fallback_reason = reason
            self.pending = None
            self.members = ()
            self.weight = 5
            self.last_category = None
            self.category_count = 0
            return snapshot

    def _apply(self, snapshot):
        members = discovered_members(snapshot, self.service)
        if len(members) != 2:
            raise ValueError("two distinct healthy endpoints required")
        data, medians = read_window(self.path, self.service, members, self.max_age)
        metrics = data["endpoints"]
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
            LOG.info(
                json.dumps(
                    {
                        "event": "rl_window",
                        "policy": self.policy,
                        "sequence": data["sequence"],
                        "effective_ms": dict(zip(members, (round(m, 2) for m in medians), strict=True)),
                        "state": list(state),
                        "action": action,
                        "weights": dict(
                            zip(members, (self.weight * 10, 100 - self.weight * 10), strict=True)
                        ),
                        "reward": round(score, 4),
                    }
                )
            )
        return weighted_snapshot(
            snapshot, self.service, dict(zip(members, (self.weight, 10 - self.weight), strict=True))
        )


SHAPE_POLICIES = ("shape-q", "shape-fixed")


class ShapeController:
    """Version 2, any number of endpoints: weights come from shape_weights(latency, beta).

    shape-q      a frozen trained model (scripts/train_shape_rl.py): a contextual bandit that
                 picks beta for (latency spread, queue pressure), or a Q-table that moves it
    shape-fixed  the same formula with one fixed beta: the baseline the agent must beat

    Safety is shared with version 1 (read_window) plus: every endpoint keeps at least `floor`
    of the traffic and no weight moves by more than `max_step` percentage points per window.
    """

    def __init__(
        self,
        path: str,
        service: str,
        *,
        policy: str = "shape-q",
        model_path: str | None = None,
        beta: float | None = None,
        floor: float = 0.05,
        max_step: int = 10,
        max_age: float = 15,
    ):
        if policy not in SHAPE_POLICIES:
            raise ValueError(f"shape policy must be one of: {', '.join(SHAPE_POLICIES)}")
        if policy == "shape-q" and (not model_path or beta is not None):
            raise ValueError("shape-q needs a model and no fixed beta")
        if policy == "shape-fixed" and (model_path or beta is None or not math.isfinite(beta) or beta < 0):
            raise ValueError("shape-fixed needs a nonnegative beta and no model")
        if not 0 < floor < 0.5 or not 1 <= max_step <= 100:
            raise ValueError("invalid floor or max_step")
        self.path, self.service, self.policy, self.max_age = Path(path), service, policy, max_age
        agent, decision = load_shape_model(model_path) if model_path else (None, {})
        self.core = ShapeCore(agent=agent, fixed_beta=beta, floor=floor, max_step=max_step, **decision)
        self.members, self.sequence, self.fallback_reason = (), -1, None

    def apply(self, snapshot):
        try:
            result = self._apply(snapshot)
            self.fallback_reason = None
            return result
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, OverflowError) as error:
            reason = f"{type(error).__name__}: {error}"
            if reason != self.fallback_reason:
                LOG.info(json.dumps({"event": "rl_fallback", "policy": self.policy, "reason": reason}))
                self.fallback_reason = reason
            self.members, self.sequence = (), -1
            self.core.reset(0)
            return snapshot

    def _apply(self, snapshot):
        members = discovered_members(snapshot, self.service)
        if len(members) < 2:
            raise ValueError("two or more endpoints required")
        if self.core.floor * len(members) >= 1:
            raise ValueError("floor too high for this many endpoints")
        data, latencies = read_window(self.path, self.service, members, self.max_age)
        if members != self.members:
            self.members, self.sequence = members, -1
            self.core.reset(len(members))
        if data["sequence"] < self.sequence:
            raise ValueError("out-of-order telemetry")
        if data["sequence"] > self.sequence:
            # Errors a server identified (rejections, timeouts it reported) are the signal to
            # shift load and already raise that endpoint's effective latency. Falling back on
            # them would hand an overloaded backend its full share again. Only errors no
            # endpoint explains mean the telemetry cannot be trusted.
            unexplained = data["errors"] - sum(data.get("endpoint_errors", {}).values())
            if unexplained / max(1, data["offered"]) > 0.1:
                raise ValueError("too many unattributed errors")
            pressure = queue_share(data, members, self.core.weights)
            errors = median_errors(data, members, latencies)
            state, action, _ = self.core.step(latencies, pressure, errors=errors)
            self.sequence = data["sequence"]
            LOG.info(
                json.dumps(
                    {
                        "event": "rl_window",
                        "policy": self.policy,
                        "sequence": data["sequence"],
                        "effective_ms": dict(zip(members, (round(m, 2) for m in latencies), strict=True)),
                        "queue_share": round(pressure, 3),
                        "state": list(state),
                        "action": action,
                        "beta": self.core.beta,
                        "weights": dict(zip(members, self.core.weights, strict=True)),
                    }
                )
            )
        return weighted_snapshot(snapshot, self.service, dict(zip(members, self.core.weights, strict=True)))


def load_shape_model(path: str) -> tuple[QAgent | None, dict]:
    """The trained rule (Q-table or bandit) and the decision settings it was trained with."""
    model = json.loads(Path(path).read_text(encoding="utf-8"))
    if list(model.get("betas", [])) != list(BETAS):
        raise ValueError("unsupported shape model")
    if model.get("kind") == "lookaside-shape-bandit-v1":
        bandit = BanditTable()
        for entry in model["contexts"]:
            context, values, counts = entry["context"], entry["values"], entry["counts"]
            if (
                len(context) != 2
                or any(type(x) is not int for x in context)
                or len(values) != len(BETAS)
                or len(counts) != len(BETAS)
                or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)
                or any(type(x) is not int or x < 0 for x in counts)
            ):
                raise ValueError("invalid bandit model")
            bandit.values[tuple(context)], bandit.counts[tuple(context)] = list(values), list(counts)
        settings = {"start_beta": float(model["start_beta"]), "hold": int(model["hold"])}
        return None, {"bandit": bandit, "dwell": int(model["dwell"]), **settings}
    if model.get("kind") != "lookaside-shape-v1":
        raise ValueError("unsupported shape model")
    agent = QAgent(epsilon=0)
    for entry in model["q"]:
        state, values = entry["state"], entry["values"]
        if (
            len(state) != 3
            or any(type(x) is not int for x in state)
            or len(values) != 3
            or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values)
        ):
            raise ValueError("invalid shape model")
        agent.q[tuple(state)] = values
    # Models written before these settings existed were trained with the defaults.
    return agent, {"start_beta": float(model.get("start_beta", 1.0)), "hold": int(model.get("hold", 2))}


def median_errors(data: dict, members, latencies: list[float]) -> list[float]:
    """Standard error of each endpoint's effective latency: 1.2533 · σ / √n for a median.

    Scaled like the effective latency itself (median / success rate). An endpoint with few
    samples gets a wide error, so a small gap on thin data is not acted upon.
    """
    errors = []
    for address, effective in zip(members, latencies, strict=True):
        values = data["endpoints"][address]
        median = statistics.median(values)
        spread = statistics.pstdev(values) if len(values) > 1 else median
        relative = 1.2533 * spread / math.sqrt(len(values)) / max(median, 1e-9)
        errors.append(relative * effective)
    return errors


def queue_share(data: dict, members, weights: list[int]) -> float:
    """Traffic-weighted share of latency spent queueing at the servers; 0 when not reported."""
    queues = data.get("endpoint_queue_ms") or {}
    waited = total = 0.0
    for address, weight in zip(members, weights, strict=True):
        samples, waits = data["endpoints"][address], queues.get(address) or []
        if not isinstance(waits, list) or any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in waits
        ):
            raise ValueError("invalid queue samples")
        total += weight * statistics.median(samples)
        waited += weight * (statistics.median(waits) if waits else 0.0)
    return min(1.0, waited / total) if total > 0 else 0.0
