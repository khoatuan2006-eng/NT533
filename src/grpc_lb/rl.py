"""Small tabular Q-learning agent for offline lookaside experiments."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

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
