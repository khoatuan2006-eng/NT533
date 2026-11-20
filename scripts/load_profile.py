"""Load that changes during a run: phases of constant or linearly ramping rate, and per-phase results.

A profile is a comma-separated list of phases, or the name of a preset:

    150:60            150 RPS for 60 s
    50-400:120        rate rising linearly from 50 to 400 RPS over 120 s
    150:60,600:10,150:90
    spike             a preset (see PRESETS); `xN` scales it: spike x10 = the spike at ten times the rate

Arrival times are deterministic, so every variant in a comparison sees exactly the same load.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

PRESETS = {
    # Low, steady, then a sudden burst at four times the rate and back: how fast does it recover?
    "spike": "150:60,600:10,150:90",
    # Load climbs in stages: do the weights follow, and where does it start to fail?
    "step-up": "100:60,200:60,400:60",
    # High load drops: does the system hand capacity back, and do weights return to even?
    "step-down": "400:60,100:90",
    # A smooth climb then a hold at the top.
    "ramp": "50-400:120,400:60",
    # Little traffic: Least Request and RL have almost nothing to act on.
    "low": "20:120",
}
# A phase after the first is settled once a window this long has no errors and a P95 close to
# the first phase's (the system's own baseline in this run).
SETTLE_WINDOW_S = 5
SETTLE_P95_FACTOR = 1.5
SETTLE_P95_SLACK_MS = 20


@dataclass(frozen=True)
class Phase:
    start_rate: float
    end_rate: float
    duration: float

    @property
    def mean_rate(self) -> float:
        return (self.start_rate + self.end_rate) / 2

    def label(self) -> str:
        if self.start_rate == self.end_rate:
            return f"{self.start_rate:g} RPS × {self.duration:g} s"
        return f"{self.start_rate:g}→{self.end_rate:g} RPS × {self.duration:g} s"


@dataclass(frozen=True)
class Profile:
    name: str
    spec: str
    phases: tuple[Phase, ...]

    @property
    def duration(self) -> float:
        return sum(phase.duration for phase in self.phases)

    @property
    def mean_rate(self) -> float:
        return self.expected_requests() / self.duration

    def bounds(self) -> list[tuple[float, float]]:
        """(start_s, end_s) of every phase."""
        result, start = [], 0.0
        for phase in self.phases:
            result.append((start, start + phase.duration))
            start += phase.duration
        return result

    def phase_at(self, t: float) -> int:
        for index, (start, end) in enumerate(self.bounds()):
            if start <= t < end:
                return index
        return len(self.phases) - 1

    def expected_requests(self) -> int:
        return sum(1 for _ in self.arrivals())

    def arrivals(self):
        """Scheduled send times in seconds from the start, in order."""
        start = 0.0
        for phase in self.phases:
            a, b, d = phase.start_rate, phase.end_rate, phase.duration
            # Arrival k (k = 0, 1, ...) is where the integrated rate reaches k:
            #   N(t) = a t + (b - a) t^2 / (2 d)
            k = 0
            while True:
                if a == b:
                    if a == 0:
                        break
                    t = k / a
                else:
                    slope = (b - a) / d
                    t = (-a + math.sqrt(a * a + 2 * slope * k)) / slope if a * a + 2 * slope * k >= 0 else d
                if t >= d:
                    break
                yield start + t
                k += 1
            start += d

    def scaled(self, factor: float) -> Profile:
        return Profile(
            f"{self.name}x{factor:g}",
            f"{self.spec} x{factor:g}",
            tuple(Phase(p.start_rate * factor, p.end_rate * factor, p.duration) for p in self.phases),
        )


PHASE = re.compile(r"(?P<a>\d+(?:\.\d+)?)(?:-(?P<b>\d+(?:\.\d+)?))?:(?P<d>\d+(?:\.\d+)?)")


def parse(text: str) -> Profile:
    """A preset name (optionally `name xN`) or an explicit list of phases."""
    text = text.strip()
    factor = 1.0
    scaled = re.fullmatch(r"(?P<base>.+?)\s*x(?P<f>\d+(?:\.\d+)?)", text)
    if scaled:
        text, factor = scaled["base"].strip(), float(scaled["f"])
        if factor <= 0:
            raise ValueError("scale factor must be positive")
    name, spec = (text, PRESETS[text]) if text in PRESETS else (_slug(text), text)
    phases = []
    for part in spec.split(","):
        match = PHASE.fullmatch(part.strip())
        if not match:
            raise ValueError(f"phase must look like 150:60 or 50-400:120, not {part!r}")
        a = float(match["a"])
        b = float(match["b"]) if match["b"] is not None else a
        d = float(match["d"])
        if d <= 0 or d > 3600 or max(a, b) > 1_000_000:
            raise ValueError(f"phase {part!r}: duration 0..3600 s, rate up to 1e6 RPS")
        phases.append(Phase(a, b, d))
    if not phases or all(p.mean_rate == 0 for p in phases):
        raise ValueError("a profile needs at least one phase with traffic")
    profile = Profile(name, spec, tuple(phases))
    return profile.scaled(factor) if factor != 1 else profile


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "profile"


def percentile(values, percent):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)] if ordered else None


def phase_summary(rows: list[dict], profile: Profile) -> list[dict]:
    """Per-phase results from one row per RPC (scheduled_s, code, latency_ms, instances)."""
    bounds = profile.bounds()
    grouped = [[] for _ in bounds]
    for row in rows:
        grouped[profile.phase_at(row["scheduled_s"])].append(row)
    results = []
    baseline_p95 = None
    for index, ((start, end), phase, group) in enumerate(zip(bounds, profile.phases, grouped, strict=True)):
        calls = [r for r in group if r["code"] != "GENERATOR_OVERFLOW"]
        ok = [r for r in calls if r["code"] == "OK"]
        latencies = [r["latency_ms"] for r in ok]
        served = [name for r in ok for name in r.get("instances", [])]
        b_total = sum(name.startswith("b-") for name in served)
        p95 = percentile(latencies, 95)
        if index == 0:
            baseline_p95 = p95
        results.append(
            {
                "phase": index + 1,
                "label": phase.label(),
                "start_s": start,
                "end_s": end,
                "offered_rps": len(group) / phase.duration,
                "goodput_rps": len(ok) / phase.duration,
                "error_percent": (1 - len(ok) / len(calls)) * 100 if calls else None,
                "generator_drops": len(group) - len(calls),
                "p50_ms": percentile(latencies, 50),
                "p95_ms": p95,
                "p99_ms": percentile(latencies, 99),
                "share_b2_percent": served.count("b-2") / b_total * 100 if b_total else None,
                "settle_s": None if index == 0 else settle_time(group, start, end, baseline_p95),
            }
        )
    return results


def settle_time(rows: list[dict], start: float, end: float, baseline_p95: float | None) -> float | None:
    """Seconds from the phase start until a clean window; None if the phase never settles."""
    if baseline_p95 is None:
        return None
    limit = max(baseline_p95 * SETTLE_P95_FACTOR, baseline_p95 + SETTLE_P95_SLACK_MS)
    ordered = sorted(rows, key=lambda r: r["scheduled_s"])
    t = start
    while t + SETTLE_WINDOW_S <= end + 1e-9:
        window = [r for r in ordered if t <= r["scheduled_s"] < t + SETTLE_WINDOW_S]
        if window and all(r["code"] == "OK" for r in window):
            p95 = percentile([r["latency_ms"] for r in window], 95)
            if p95 is not None and p95 <= limit:
                return t - start
        t += 1
    return None


def timeseries(rows: list[dict], duration: float) -> list[dict]:
    """One line per second: offered, successful, failed, P95 of successful (for plots)."""
    seconds = [[] for _ in range(max(1, math.ceil(duration)))]
    for row in rows:
        index = min(len(seconds) - 1, max(0, int(row["scheduled_s"])))
        seconds[index].append(row)
    result = []
    for second, group in enumerate(seconds):
        ok = [r["latency_ms"] for r in group if r["code"] == "OK"]
        result.append(
            {
                "second": second,
                "offered": len(group),
                "ok": len(ok),
                "failed": len(group) - len(ok),
                "p95_ms": percentile(ok, 95),
            }
        )
    return result
