"""Train a toy two-backend Q-learning policy; simulation only, never deploys."""

import argparse
import json
import math
import random

from grpc_lb.rl import QAgent, reward


def run(episodes: int, seed: int):
    rng = random.Random(seed)
    agent = QAgent(seed=seed, epsilon=0.2)
    for _ in range(episodes):
        # Two capacity regimes; randomize load and the slow endpoint each episode.
        slow = rng.randrange(2)
        offered = rng.randint(60, 160)
        capacities = [100, 100]
        capacities[slow] = 50
        weight = 5
        for step in range(30):
            state = (slow, offered // 40, weight)
            allowed = tuple(a for a in (-1, 0, 1) if 1 <= weight + a <= 9)
            action = agent.choose(state, explore=True, allowed=allowed)
            new_weight = min(9, max(1, weight + action))
            actual_action = new_weight - weight
            assigned = [round(offered * new_weight / 10), offered - round(offered * new_weight / 10)]
            successful = sum(min(assigned[i], capacities[i]) for i in range(2))
            errors = offered - successful
            utilization = max(assigned[i] / capacities[i] for i in range(2))
            p95 = 100 * (1 + max(0, utilization - 0.7) * 3)
            r = reward(offered, successful, errors, p95, 150, actual_action)
            next_state = (slow, offered // 40, new_weight)
            next_allowed = tuple(a for a in (-1, 0, 1) if 1 <= new_weight + a <= 9)
            agent.update(state, action, r, next_state, terminated=step == 29, next_allowed=next_allowed)
            weight = new_weight
    return {
        "schema_version": 1,
        "kind": "toy-q-learning",
        "seed": seed,
        "episodes": episodes,
        "q": [{"state": list(k), "values": v} for k, v in sorted(agent.q.items())],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.episodes <= 0 or not math.isfinite(args.episodes):
        parser.error("episodes must be positive")
    with open(args.output, "w", encoding="utf-8") as output:
        json.dump(run(args.episodes, args.seed), output, indent=2)
    print(f"Saved experimental policy to {args.output}")


if __name__ == "__main__":
    main()
