"""Train tabular Q policy from simulated observable latency windows, not backend labels."""

import argparse
import json
import random
from pathlib import Path

from grpc_lb.rl import QAgent, reward


def train(episodes, seed):
    rng = random.Random(seed)
    agent = QAgent(alpha=0.15, gamma=0.8, epsilon=0.25, seed=seed)
    for episode in range(episodes):
        regime = rng.randrange(3)
        means = ([16, 32], [32, 16], [24, 24])[regime]
        weight = rng.randint(1, 9)
        category = 0 if regime == 0 else 2 if regime == 1 else 1
        for step in range(15):
            state = (category, weight)
            allowed = tuple(a for a in (-1, 0, 1) if 1 <= weight + a <= 9)
            action = agent.choose(state, explore=True, allowed=allowed)
            updated = weight + action
            samples = [
                max(1, rng.gauss(means[0 if rng.random() < updated / 10 else 1], 3)) for _ in range(80)
            ]
            p95 = sorted(samples)[75]
            score = reward(80, 80, 0, p95, 50, action, mean_ms=sum(samples) / len(samples))
            next_state = (category, updated)
            next_allowed = tuple(a for a in (-1, 0, 1) if 1 <= updated + a <= 9)
            agent.update(state, action, score, next_state, terminated=step == 14, next_allowed=next_allowed)
            weight = updated
        agent.epsilon = max(0.05, 0.25 * (1 - episode / episodes))
    return {
        "kind": "lookaside-q-v1",
        "seed": seed,
        "episodes": episodes,
        "q": [{"state": list(state), "values": values} for state, values in sorted(agent.q.items())],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.episodes < 1:
        parser.error("episodes must be positive")
    Path(args.output).write_text(json.dumps(train(args.episodes, args.seed), indent=2), encoding="utf-8")
    print(f"Saved policy: {args.output}")


if __name__ == "__main__":
    main()
