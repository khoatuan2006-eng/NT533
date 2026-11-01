"""Audit tabular RL convergence in a stationary, synthetic two-instance environment."""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from grpc_lb.rl import QAgent, reward


def policy(agent):
    return {
        f"{category}:{weight}": agent.choose(
            (category, weight), allowed=tuple(a for a in (-1, 0, 1) if 3 <= weight + a <= 7)
        )
        for category in (0, 1, 2)
        for weight in range(3, 8)
    }


def run(seed, updates, checkpoint):
    rng = random.Random(seed)
    agent = QAgent(alpha=0.2, gamma=0.8, epsilon=0.15, seed=seed)
    visits = Counter()
    history = []
    last = None
    last_q = None
    category = 0
    weight = 5
    for step in range(1, updates + 1):
        if step % 20 == 1:
            category = rng.randrange(3)
            weight = rng.randint(3, 7)
        state = (category, weight)
        allowed = tuple(a for a in (-1, 0, 1) if 3 <= weight + a <= 7)
        action = agent.choose(state, explore=True, allowed=allowed)
        updated = weight + action
        means = ((16, 32), (24, 24), (32, 16))[category]
        mean = (updated * means[0] + (10 - updated) * means[1]) / 10 + rng.gauss(0, 0.8)
        score = reward(80, 80, 0, 40, 50, action, mean_ms=max(0, mean))
        # When replicas are equivalent, penalize needless skew.
        if category == 1:
            score -= 0.02 * abs(updated - 5)
        next_state = (category, updated)
        next_allowed = tuple(a for a in (-1, 0, 1) if 3 <= updated + a <= 7)
        visits[(state, action)] += 1
        agent.alpha = min(0.3, 1 / visits[(state, action)] ** 0.65)
        agent.update(state, action, score, next_state, next_allowed=next_allowed)
        weight = updated
        if step % checkpoint == 0:
            current = policy(agent)
            flat = {
                (state, action): values[action + 1]
                for state, values in agent.q.items()
                for action in (-1, 0, 1)
                if 3 <= state[1] + action <= 7
            }
            changes = None if last is None else sum(current[k] != last[k] for k in current)
            delta = None if last_q is None else max(abs(flat[k] - last_q.get(k, 0)) for k in flat)
            history.append(
                {"updates": step, "policy_changes": changes, "max_q_change": delta, "policy": current}
            )
            last, last_q = current, flat
    return {
        "seed": seed,
        "history": history,
        "min_visits": min(visits.values()),
        "model": {
            "kind": "lookaside-q-v1",
            "seed": seed,
            "q": [{"state": list(s), "values": values} for s, values in sorted(agent.q.items())],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--updates", type=int, default=300000)
    parser.add_argument("--checkpoint", type=int, default=50000)
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 19, 31])
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if args.updates < args.checkpoint or args.checkpoint < 1 or args.updates % args.checkpoint:
        parser.error("updates must be a positive multiple of checkpoint")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    for seed in args.seeds:
        result = run(seed, args.updates, args.checkpoint)
        (out / f"model-{seed}.json").write_text(json.dumps(result.pop("model"), indent=2), encoding="utf-8")
        runs.append(result)
    (out / "convergence.json").write_text(
        json.dumps({"parameters": vars(args), "runs": runs}, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            [{"seed": r["seed"], "last": r["history"][-1], "min_visits": r["min_visits"]} for r in runs]
        )
    )


if __name__ == "__main__":
    main()
