"""Small tabular Q-learning agent for offline lookaside experiments."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from statistics import NormalDist

Actions = (-1, 0, 1)


@dataclass
class QAgent:
    alpha: float = 0.1
    gamma: float = 0.9
    epsilon: float = 0.1
    seed: int = 0
    q: dict[tuple[int, ...], list[float]] = field(default_factory=dict)

    def __post_init__(self):
        if not all(math.isfinite(x) and 0 <= x <= 1 for x in (self.alpha, self.gamma, self.epsilon)):
            raise ValueError("learning parameters must be finite and in [0, 1]")
        self.random = random.Random(self.seed)

    def choose(self, state: tuple[int, ...], *, explore: bool = False, allowed=Actions) -> int:
        values = self.q.setdefault(state, [0.0, 0.0, 0.0])
        if not allowed or any(action not in Actions for action in allowed):
            raise ValueError("invalid allowed actions")
        if explore and self.random.random() < self.epsilon:
            return self.random.choice(allowed)
        # Prefer holding on ties so an untrained agent does not shift traffic.
        return max(allowed, key=lambda action: (values[Actions.index(action)], action == 0))

    def update(self, state, action, reward, next_state, *, terminated=False, next_allowed=Actions):
        if (
            action not in Actions
            or not math.isfinite(reward)
            or not next_allowed
            or any(a not in Actions for a in next_allowed)
        ):
            raise ValueError("invalid action or reward")
        values = self.q.setdefault(state, [0.0, 0.0, 0.0])
        next_values = self.q.setdefault(next_state, [0.0, 0.0, 0.0])
        target = reward + (
            0 if terminated else self.gamma * max(next_values[Actions.index(a)] for a in next_allowed)
        )
        index = Actions.index(action)
        values[index] += self.alpha * (target - values[index])


def reward(
    offered: int,
    successful: int,
    errors: int,
    p95_ms: float,
    target_ms: float,
    change: int,
    *,
    mean_ms: float = 0,
) -> float:
    """Bounded illustrative reward; errors include failed and overdue requests."""
    if (
        type(offered) is not int
        or type(successful) is not int
        or type(errors) is not int
        or offered <= 0
        or min(successful, errors) < 0
        or successful + errors > offered
        or not math.isfinite(p95_ms)
        or not math.isfinite(target_ms)
        or p95_ms < 0
        or not math.isfinite(mean_ms)
        or mean_ms < 0
        or target_ms <= 0
        or change not in Actions
    ):
        raise ValueError("invalid reward inputs")
    return (
        successful / offered
        - 2 * errors / offered
        - min(2, max(0, p95_ms / target_ms - 1))
        - 0.005 * abs(change)
        - min(2, mean_ms / target_ms)
    )


# ---- Version 2: the agent tunes how aggressively a formula shifts load -------------------
#
# A formula turns per-endpoint latency into weights; the agent only chooses its aggressiveness
# beta. The action space (lower / hold / raise beta) stays three actions for any number of
# endpoints, and the state adds the load pressure that a fixed formula cannot react to.

BETAS = (0.0, 0.5, 1.0, 2.0, 4.0)
SPREAD_EDGES = (1.3, 2.0, 4.0)  # slowest / fastest effective latency
PRESSURE_EDGES = (0.1, 0.4)  # share of time requests wait in server queues


def bucket(value: float, edges: tuple[float, ...]) -> int:
    return sum(value >= edge for edge in edges)


def shape_weights(latencies: list[float], beta: float, floor: float) -> list[float]:
    """Fractions summing to 1: w_i ∝ exp(-beta (l_i / l_min - 1)), then each at least floor."""
    if len(latencies) < 2 or any(not math.isfinite(x) or x <= 0 for x in latencies):
        raise ValueError("two or more positive latencies required")
    if not 0 <= floor < 1 / len(latencies) or not math.isfinite(beta) or beta < 0:
        raise ValueError("invalid beta or floor")
    fastest = min(latencies)
    raw = [math.exp(-beta * min(50.0, value / fastest - 1)) for value in latencies]
    total = sum(raw)
    return [floor + (1 - floor * len(raw)) * value / total for value in raw]


def percent_weights(fractions: list[float]) -> list[int]:
    """Integers 1..100 summing to 100 (largest remainder), as xDS locality weights."""
    exact = [fraction * 100 for fraction in fractions]
    result = [max(1, int(value)) for value in exact]
    order = sorted(range(len(exact)), key=lambda i: exact[i] - int(exact[i]), reverse=True)
    index = 0
    while sum(result) < 100:
        result[order[index % len(order)]] += 1
        index += 1
    while sum(result) > 100:
        largest = max(range(len(result)), key=result.__getitem__)
        result[largest] -= 1
    return result


# Chance that any one window flags an equal endpoint as different, across all endpoints.
FALSE_ALARM = 0.05


def significance(endpoints: int) -> float:
    """Two-sided Bonferroni threshold in standard errors: about 2.4 at 3 endpoints, 2.8 at 10.

    With many endpoints, one of them crosses a fixed threshold by chance in most windows; the
    threshold therefore grows with the number of comparisons.
    """
    return NormalDist().inv_cdf(1 - FALSE_ALARM / (2 * endpoints))


def shrink_latencies(latencies: list[float], errors: list[float], k: float | None = None) -> list[float]:
    """Pull each latency towards the common median by k standard errors (a soft threshold).

    With few samples per endpoint (many endpoints sharing the load) noise alone makes some look
    faster; acting on that herds traffic onto them and overloads them. Differences smaller than
    k standard errors are treated as zero, larger ones keep only the part beyond the noise.
    """
    if len(errors) != len(latencies) or any(not math.isfinite(e) or e < 0 for e in errors):
        raise ValueError("one nonnegative standard error per latency required")
    k = significance(len(latencies)) if k is None else k
    center = statistics.median(latencies)
    return [
        center + math.copysign(max(0.0, abs(value - center) - k * error), value - center)
        for value, error in zip(latencies, errors, strict=True)
    ]


def shape_context(latencies: list[float], queue_share: float) -> tuple[int, int]:
    """(latency spread bucket, load pressure bucket) observed in one window."""
    return (
        bucket(max(latencies) / min(latencies), SPREAD_EDGES),
        bucket(queue_share, PRESSURE_EDGES),
    )


def relative_reward(mean_ms: float, baseline_ms: float, extra_errors: float, change: int) -> float:
    """Improvement over Round Robin under the same input, so offered load cancels out.

    mean_ms / baseline_ms: mean latency of successful requests with this policy / with Round
    Robin; extra_errors: error fraction minus Round Robin's (negative when this policy fails less).
    """
    if not all(math.isfinite(x) for x in (mean_ms, baseline_ms, extra_errors)) or baseline_ms <= 0:
        raise ValueError("invalid reward inputs")
    return (baseline_ms - mean_ms) / baseline_ms - 4 * extra_errors - 0.01 * abs(change)


def step_towards(current: list[int], target: list[int], max_step: int, floor: float) -> list[int]:
    """Move each weight at most max_step points towards target, keeping the floor and a sum of 100."""
    deltas = [max(-max_step, min(max_step, new - old)) for old, new in zip(current, target, strict=True)]
    # Clipping breaks the zero sum: shrink whichever side is larger so gains equal losses.
    gained = sum(d for d in deltas if d > 0)
    lost = -sum(d for d in deltas if d < 0)
    moved = min(gained, lost)
    deltas = [d * moved / gained if d > 0 else d * moved / lost if d < 0 else 0 for d in deltas]
    values = [max(floor * 100, old + d) for old, d in zip(current, deltas, strict=True)]
    return percent_weights([value / sum(values) for value in values])


class BanditTable:
    """Contextual bandit: mean reward of each beta in each (spread, pressure) context.

    No bootstrapping from future values, unlike Q-learning: each estimate is a plain average of
    observed rewards, which keeps the variance between training runs low.
    """

    def __init__(self, *, epsilon: float = 0.0, seed: int = 0):
        self.values: dict[tuple[int, int], list[float]] = {}
        self.counts: dict[tuple[int, int], list[int]] = {}
        self.epsilon = epsilon
        self.random = random.Random(seed)

    def choose(self, context, default: int, *, explore: bool = False) -> int:
        if explore and self.random.random() < self.epsilon:
            return self.random.randrange(len(BETAS))
        counts = self.counts.get(context)
        if not counts or not any(counts):
            return default  # never seen: keep the trained default
        values = self.values[context]
        seen = [i for i, n in enumerate(counts) if n]
        return max(seen, key=lambda i: (values[i], i == default))

    def update(self, context, arm: int, reward: float) -> None:
        if not 0 <= arm < len(BETAS) or not math.isfinite(reward):
            raise ValueError("invalid arm or reward")
        values = self.values.setdefault(context, [0.0] * len(BETAS))
        counts = self.counts.setdefault(context, [0] * len(BETAS))
        counts[arm] += 1
        values[arm] += (reward - values[arm]) / counts[arm]


class ShapeCore:
    """One window of the version-2 decision, identical in the simulator and the control plane.

    Exactly one decision rule:
      agent       Q-table over (spread, pressure, beta index): moves beta one step at a time
      bandit      BanditTable: picks any beta for the context, then keeps it `dwell` windows
      fixed_beta  beta never changes (the baselines)
    The learned rules start from `start_beta` and only act after `hold` consecutive windows in
    the same context. start_beta, hold and dwell are part of a trained model: the control plane
    must use the values it was trained with.
    """

    def __init__(
        self,
        *,
        agent: QAgent | None = None,
        bandit: BanditTable | None = None,
        fixed_beta: float | None = None,
        floor=0.05,
        max_step=10,
        start_beta: float = 1.0,
        hold: int = 2,
        dwell: int = 3,
    ):
        if sum(x is not None for x in (agent, bandit, fixed_beta)) != 1:
            raise ValueError("exactly one of agent, bandit and fixed_beta")
        if (
            start_beta not in BETAS
            or type(hold) is not int
            or hold < 1
            or type(dwell) is not int
            or dwell < 1
        ):
            raise ValueError("start_beta must be one of BETAS, hold >= 1 and dwell >= 1")
        self.agent, self.bandit, self.fixed_beta = agent, bandit, fixed_beta
        self.floor, self.max_step = floor, max_step
        self.start_index, self.hold, self.dwell = BETAS.index(start_beta), hold, dwell
        self.reset(0)

    def reset(self, endpoints: int) -> None:
        self.weights = percent_weights([1 / endpoints] * endpoints) if endpoints else []
        self.beta_index = self.start_index
        self.last_context, self.context_count = None, 0
        self.since_decision = self.dwell  # free to decide as soon as the context is stable
        self.decision = None  # (context, arm) chosen in the latest step, for bandit training

    @property
    def beta(self) -> float:
        return self.fixed_beta if self.fixed_beta is not None else BETAS[self.beta_index]

    def state(self, context) -> tuple[int, int, int]:
        return (*context, self.beta_index)

    def step(
        self,
        latencies: list[float],
        pressure: float,
        *,
        errors: list[float] | None = None,
        explore: bool = False,
    ):
        """Update weights from one window; returns (state before, action, context).

        errors: standard error of each latency; when given, differences within the noise are
        ignored (shrink_latencies) for the context and the formula alike.
        """
        if errors is not None:
            latencies = shrink_latencies(latencies, errors)
        context = shape_context(latencies, pressure)
        if context == self.last_context:
            self.context_count += 1
        else:
            self.last_context, self.context_count = context, 1
        before, action = self.state(context), 0
        self.decision = None
        self.since_decision += 1
        if self.agent is not None and self.context_count >= self.hold:
            allowed = tuple(a for a in Actions if 0 <= self.beta_index + a < len(BETAS))
            action = self.agent.choose(before, explore=explore, allowed=allowed)
            self.beta_index += action
        elif (
            self.bandit is not None and self.context_count >= self.hold and self.since_decision >= self.dwell
        ):
            arm = self.bandit.choose(context, self.start_index, explore=explore)
            action, self.beta_index, self.since_decision = arm - self.beta_index, arm, 0
            self.decision = (context, arm)
        target = percent_weights(shape_weights(latencies, self.beta, self.floor))
        self.weights = step_towards(self.weights, target, self.max_step, self.floor)
        return before, action, context
