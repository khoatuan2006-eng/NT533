"""Train and check the version-2 RL agent (beta tuning) in a queueing simulation.

Default learner: a contextual bandit that picks beta per (latency spread, queue pressure) and
keeps it 5 windows; --method q trains the Q-learning variant instead.

Each backend is an M/M/c queue: c parallel slots, service time d, plus an optional path delay.
Latency therefore rises with the share a backend receives, which is what a fixed formula cannot
anticipate. The agent sees exactly what the control plane sees (effective latency per endpoint,
queue share) through grpc_lb.rl.ShapeCore, so the trained table runs unchanged in production.

Reward is the improvement over Round Robin under the same input in the same window (the
input-dependent baseline of Mao et al., ICLR 2019), so the offered load cancels out.

Training and evaluation use disjoint seeds. The report compares the agent with every fixed
beta, including the single best one: the agent only adds value if it beats that.

    python scripts/train_shape_rl.py --output-dir results/shape-rl

Seeds: several training seeds; the validation seed picks one model; the test seed is run once,
for that model only, and is the number to report. Do not change the design after reading it.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path

from grpc_lb.rl import BETAS, BanditTable, QAgent, ShapeCore, relative_reward

WINDOWS = 40
WARMUP = 5
MAX_UTILIZATION = 0.97
KINDS = ("even", "path-slow", "processing-slow", "uneven-capacity", "flapping", "load-step")
TRAIN_SIZES = (2, 3, 4, 6, 8, 10)
# Like the localhost lab: total capacity stays fixed as backends are added, so each backend
# serves fewer requests and its window median is noisier (about 240 RPS in all).
SERVICE_MS_AT_THREE = 50.0
WINDOW_S = 5.0
LATENCY_CV = 0.7  # spread of single-request latency relative to its mean
NOISE_AWARE = True  # --ignore-noise turns the shrinking off, to measure what it adds
AFFECTED = 0.3  # share of backends a fault touches, so a fault matters as much at 10 as at 3


def affected(n: int) -> int:
    return max(1, round(AFFECTED * n))


def erlang_c_wait(slots: int, service_ms: float, arrival_per_ms: float) -> tuple[float, float]:
    """(mean queue wait in ms, rejected fraction) of an M/M/c queue; overload is clipped."""
    offered = arrival_per_ms * service_ms  # in Erlangs
    rejected = 0.0
    if offered >= MAX_UTILIZATION * slots:
        rejected = 1 - MAX_UTILIZATION * slots / offered
        offered = MAX_UTILIZATION * slots
    if offered <= 0:
        return 0.0, 0.0
    total, term = 0.0, 1.0
    for k in range(slots):
        if k:
            term *= offered / k
        total += term
    top = term * offered / slots * slots / (slots - offered)
    probability = top / (total + top)
    return probability * service_ms / (slots - offered), rejected


class Scenario:
    def __init__(self, rng: random.Random, kind: str, sizes=TRAIN_SIZES):
        self.kind = kind
        self.n = rng.choice(tuple(sizes))
        base = SERVICE_MS_AT_THREE * self.n / 3
        self.service = [base * rng.uniform(0.9, 1.1) for _ in range(self.n)]
        # "even" is the control case: identical backends, nothing to avoid.
        same = kind == "even" or (kind != "uneven-capacity" and rng.random() < 0.5)
        self.slots = [4] * self.n if same else [rng.choice((2, 4, 8)) for _ in range(self.n)]
        if kind == "uneven-capacity":
            order = rng.sample(range(self.n), self.n)
            k = affected(self.n)
            for i in order[:k]:
                self.slots[i] = 8
            for i in order[k : 2 * k] if self.n > 1 else ():
                self.slots[i] = 2
        capacity = sum(c / d for c, d in zip(self.slots, self.service, strict=True))
        self.load = [rng.uniform(0.2, 0.9) * capacity] * WINDOWS
        if kind == "load-step":
            step = rng.uniform(0.2, 0.9) * capacity
            self.load = [self.load[0]] * (WINDOWS // 2) + [step] * (WINDOWS - WINDOWS // 2)
        self.slow = rng.sample(range(self.n), affected(self.n))
        self.delay = base * rng.choice((2.0, 5.0))
        self.factor = rng.choice((2.0, 3.0))
        self.period = rng.choice((6, 10))
        self.noise = random.Random(rng.random())

    def condition(self, window: int) -> tuple[list[float], list[float]]:
        """(service ms, path delay ms) per backend in this window."""
        service, path = list(self.service), [0.0] * self.n
        active = self.kind != "flapping" or (window // self.period) % 2 == 0
        for i in self.slow:
            if self.kind in ("path-slow", "flapping") and active:
                path[i] = self.delay
            if self.kind == "processing-slow":
                service[i] *= self.factor
        return service, path

    def measure(self, window: int, weights: list[float]):
        """True (mean ms, error fraction) and what telemetry would show for these weights."""
        service, path = self.condition(window)
        latency, errors, queues = [], [], []
        for i in range(self.n):
            wait, rejected = erlang_c_wait(self.slots[i], service[i], self.load[window] * weights[i])
            latency.append(service[i] + wait + path[i])
            errors.append(rejected)
            queues.append(wait)
        served = [w * (1 - e) for w, e in zip(weights, errors, strict=True)]
        mean = sum(s * x for s, x in zip(served, latency, strict=True)) / max(1e-9, sum(served))
        error = sum(w * e for w, e in zip(weights, errors, strict=True))
        # A window median over n samples is off by about 1.2533 · CV / √n of the mean.
        samples = [max(3.0, self.load[window] * w * WINDOW_S * 1000) for w in weights]
        sigma = [1.2533 * LATENCY_CV / math.sqrt(n) for n in samples]
        observed = [
            x * math.exp(self.noise.gauss(0, s)) / max(0.05, 1 - e)
            for x, s, e in zip(latency, sigma, errors, strict=True)
        ]
        standard_errors = [o * s for o, s in zip(observed, sigma, strict=True)]
        total = sum(w * x for w, x in zip(weights, latency, strict=True))
        pressure = sum(w * q for w, q in zip(weights, queues, strict=True)) / total
        return mean, error, observed, pressure, standard_errors


def run_episode(
    scenario: Scenario, core: ShapeCore, *, learn: bool = False, noise_aware: bool | None = None
) -> list[float]:
    """Per-window relative improvement over Round Robin; updates the agent when learning."""
    if noise_aware is None:
        noise_aware = NOISE_AWARE
    core.reset(scenario.n)
    uniform = [1 / scenario.n] * scenario.n
    improvements, pending = [], None
    choice, earned = None, []  # bandit: the active (context, arm) and the rewards since it
    for window in range(WINDOWS):
        weights = [w / 100 for w in core.weights]
        mean, error, observed, pressure, standard_errors = scenario.measure(window, weights)
        rr_mean, rr_error, *_ = scenario.measure(window, uniform)
        score = relative_reward(mean, rr_mean, error - rr_error, pending[1] if pending else 0)
        if window >= WARMUP:
            improvements.append(score)
        earned.append(score)
        state, action, _ = core.step(
            observed, pressure, errors=standard_errors if noise_aware else None, explore=learn
        )
        if learn and core.agent is not None and pending is not None:
            allowed = tuple(a for a in (-1, 0, 1) if 0 <= state[2] + a < len(BETAS))
            core.agent.update(pending[0], pending[1], score, state, next_allowed=allowed)
        if core.decision is not None:
            # Credit a bandit choice with the mean reward of the windows it was in force.
            if learn and choice is not None and earned:
                core.bandit.update(*choice, statistics.mean(earned))
            choice, earned = core.decision, []
        pending = (state, action)
    if learn and core.bandit is not None and choice is not None and earned:
        core.bandit.update(*choice, statistics.mean(earned))
    return improvements


def train(episodes: int, seed: int, decision: dict | None = None, method: str = "q", sizes=TRAIN_SIZES):
    """A trained Q-table (method q) or bandit table (method bandit)."""
    decision = dict(decision or {})
    if method == "q":
        decision.pop("dwell", None)
        learner = QAgent(alpha=0.1, gamma=0.3, epsilon=0.3, seed=seed)
        core = ShapeCore(agent=learner, **decision)
    elif method == "bandit":
        learner = BanditTable(epsilon=0.3, seed=seed)
        core = ShapeCore(bandit=learner, **decision)
    else:
        raise ValueError("method must be q or bandit")
    rng = random.Random(seed)
    for episode in range(episodes):
        learner.epsilon = max(0.02, 0.3 * (1 - episode / (0.8 * episodes)))
        run_episode(Scenario(rng, rng.choice(KINDS), sizes), core, learn=True)
    return learner


def evaluate(learner, episodes: int, seed: int, decision: dict | None = None, sizes=TRAIN_SIZES) -> dict:
    policies = {f"beta={beta:g}": ShapeCore(fixed_beta=beta) for beta in BETAS}
    learner.epsilon = 0
    decision = dict(decision or {})
    if isinstance(learner, BanditTable):
        policies["rl"] = ShapeCore(bandit=learner, **decision)
    else:
        decision.pop("dwell", None)
        policies["rl"] = ShapeCore(agent=learner, **decision)
    scores: dict[str, dict[str, list[float]]] = {name: {kind: [] for kind in KINDS} for name in policies}
    rng = random.Random(seed)
    for _ in range(episodes):
        state = rng.getstate()
        kind = rng.choice(KINDS)
        for name, core in policies.items():
            rng.setstate(state)  # every policy faces the same scenario and noise
            rng.choice(KINDS)
            scores[name][kind].append(statistics.mean(run_episode(Scenario(rng, kind, sizes), core)))
    summary = {
        name: {kind: statistics.mean(values) for kind, values in by_kind.items()}
        | {"all": statistics.mean(v for values in by_kind.values() for v in values)}
        for name, by_kind in scores.items()
    }
    fixed = [name for name in policies if name != "rl"]
    best = max(fixed, key=lambda name: summary[name]["all"])
    # Upper bound for any beta selector: knowing the scenario type, pick its best fixed beta.
    oracle = {kind: max(summary[name][kind] for name in fixed) for kind in KINDS}
    counts = {kind: len(scores["rl"][kind]) for kind in KINDS}
    oracle["all"] = sum(oracle[kind] * counts[kind] for kind in KINDS) / sum(counts.values())
    summary["oracle"] = oracle
    return {"summary": summary, "best_fixed": best, "episodes": episodes, "seed": seed, "sizes": list(sizes)}


def table(result: dict, title: str) -> list[str]:
    summary, best = result["summary"], result["best_fixed"]
    lines = [
        f"## {title}",
        "",
        f"{result['episodes']} kịch bản, seed {result['seed']}, số instance "
        f"{', '.join(map(str, result['sizes']))}.",
        "",
        "| Chính sách | " + " | ".join(KINDS) + " | Tất cả |",
        "|---|" + "---:|" * (len(KINDS) + 1),
    ]
    for name, row in summary.items():
        mark = (
            " (β cố định tốt nhất)"
            if name == best
            else " (cận trên: biết loại kịch bản, chọn β tốt nhất của nó)"
            if name == "oracle"
            else ""
        )
        cells = " | ".join(f"{row[kind]:.3f}" for kind in (*KINDS, "all"))
        lines.append(f"| {name}{mark} | {cells} |")
    gap = summary["rl"]["all"] - summary[best]["all"]
    lines += ["", f"RL − {best}: **{gap:+.3f}** trên toàn bộ.", ""]
    return lines


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--episodes", type=int, default=20000)
    parser.add_argument("--eval-episodes", type=int, default=600)
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 8, 9, 10, 11], help="Training seeds")
    parser.add_argument("--validation-seed", type=int, default=1007, help="Chooses the model")
    parser.add_argument(
        "--test-seed", type=int, default=5051, help="Reported once, for the chosen model only"
    )
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=list(TRAIN_SIZES), help="Backend counts to train and score on"
    )
    parser.add_argument(
        "--report-sizes",
        type=int,
        nargs="*",
        default=[3, 10],
        help="Also score the chosen model at each size alone",
    )
    parser.add_argument("--start-beta", type=float, default=2.0, choices=BETAS, help="Agent's initial beta")
    parser.add_argument("--hold", type=int, default=2, help="Windows in the same context before beta moves")
    parser.add_argument(
        "--method", choices=("q", "bandit"), default="bandit", help="Q-learning or contextual bandit"
    )
    parser.add_argument("--dwell", type=int, default=5, help="Bandit: windows a chosen beta stays in force")
    parser.add_argument("--no-test", action="store_true", help="Validation only: use while comparing designs")
    parser.add_argument(
        "--ignore-noise", action="store_true", help="Act on raw latencies (no shrinking by standard error)"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    global NOISE_AWARE
    NOISE_AWARE = not args.ignore_noise
    held_out = {args.validation_seed, args.test_seed}
    if args.episodes < 1 or args.eval_episodes < 1 or len(held_out) < 2 or held_out & set(args.seeds):
        parser.error("positive episode counts; training, validation and test seeds must all differ")
    if any(n < 2 or n > 20 for n in [*args.sizes, *args.report_sizes]):
        parser.error("backend counts must be in 2..20")
    sizes = tuple(args.sizes)
    decision = {"start_beta": args.start_beta, "hold": args.hold}
    if args.method == "bandit":
        decision["dwell"] = args.dwell
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidates = []
    for seed in args.seeds:
        agent = train(args.episodes, seed, decision, args.method, sizes)
        validation = evaluate(agent, args.eval_episodes, args.validation_seed, decision, sizes)
        gap = validation["summary"]["rl"]["all"] - validation["summary"][validation["best_fixed"]]["all"]
        print(f"seed {seed}: RL − {validation['best_fixed']} = {gap:+.3f} (kiểm định)", flush=True)
        candidates.append((gap, seed, agent, validation))
    gap, seed, agent, validation = max(candidates, key=lambda item: item[0])
    test = None if args.no_test else evaluate(agent, args.eval_episodes, args.test_seed, decision, sizes)
    # Per-size tables: the test seed when it runs, otherwise the validation seed.
    by_size_seed = args.validation_seed if args.no_test else args.test_seed
    by_size = {
        n: evaluate(agent, args.eval_episodes, by_size_seed, decision, (n,)) for n in args.report_sizes
    }
    model = {
        "kind": "lookaside-shape-v1" if args.method == "q" else "lookaside-shape-bandit-v1",
        "betas": list(BETAS),
        "seed": seed,
        "episodes": args.episodes,
        **decision,
    }
    if args.method == "q":
        model["q"] = [{"state": list(state), "values": values} for state, values in sorted(agent.q.items())]
    else:
        model["contexts"] = [
            {"context": list(context), "values": agent.values[context], "counts": agent.counts[context]}
            for context in sorted(agent.counts)
        ]
    (args.output_dir / "model.json").write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    record = {
        "validation_gaps": {s: g for g, s, _, _ in candidates},
        "chosen_seed": seed,
        "validation": validation,
        "test": test,
        "by_size": {str(n): result for n, result in by_size.items()},
    }
    (args.output_dir / "simulation.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    gaps = sorted(g for g, *_ in candidates)
    lines = [
        "# RL v2 trong mô phỏng",
        "",
        "Ô là cải thiện trung bình so với Round Robin cùng đầu vào (0,10 = latency thấp hơn 10%); "
        "lỗi nhiều hơn Round Robin bị trừ 4 lần.",
        "",
        f"Huấn luyện {len(args.seeds)} seed ({', '.join(map(str, args.seeds))}), mỗi seed {args.episodes} "
        f"kịch bản; cách học {args.method}, β khởi đầu {args.start_beta:g}, giữ {args.hold} cửa sổ"
        + (f", mỗi lựa chọn giữ {args.dwell} cửa sổ" if args.method == "bandit" else "")
        + ". RL − β cố định tốt nhất "
        f"trên tập kiểm định: median {statistics.median(gaps):+.3f}, từ {gaps[0]:+.3f} đến {gaps[-1]:+.3f}. "
        f"Chọn seed {seed} theo tập kiểm định; tập kiểm tra chỉ chạy cho model đã chọn.",
        "",
        *(
            table(test, "Tập kiểm tra (số dùng cho báo cáo)")
            if test
            else ["Không chạy tập kiểm tra (--no-test).", ""]
        ),
        *(
            line
            for n, result in by_size.items()
            for line in table(
                result,
                f"{n} instance ({'tập kiểm tra' if test else 'tập kiểm định'})",
            )
        ),
        *table(validation, "Tập kiểm định (đã dùng để chọn model, không báo cáo như kết quả)"),
        "Đây là mô phỏng: chỉ dùng để chọn model và dự đoán, không thay cho số đo trên lab.",
        "",
    ]
    text = "\n".join(lines)
    (args.output_dir / "simulation.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
