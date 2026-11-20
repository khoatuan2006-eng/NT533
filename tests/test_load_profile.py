"""Changing-load profiles: schedules, per-phase results, and how both load tools report them."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from grpc_lb.service_runtime import start_service
from grpc_lb.topology import ServiceConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import load_profile  # noqa: E402
import run_load  # noqa: E402
import scale_probe  # noqa: E402


def test_constant_phases_send_exactly_rate_times_duration():
    profile = load_profile.parse("spike")
    times = list(profile.arrivals())
    assert len(times) == 150 * 60 + 600 * 10 + 150 * 90
    assert times == sorted(times) and times[-1] < profile.duration == 160
    assert profile.phase_at(65) == 1 and profile.phase_at(159.9) == 2


@pytest.mark.parametrize("spec, expected", [("50-400:120", 27000), ("400-50:120", 27000), ("0-100:10", 500)])
def test_ramps_follow_the_integrated_rate(spec, expected):
    times = list(load_profile.parse(spec).arrivals())
    assert abs(len(times) - expected) <= 1
    assert times == sorted(times)
    phase = load_profile.parse(spec).phases[0]
    first_half = sum(t < phase.duration / 2 for t in times)
    # A rising ramp sends fewer requests in its first half than in its second, and vice versa.
    assert (first_half < len(times) / 2) == (phase.start_rate < phase.end_rate)


def test_scaling_and_pauses():
    profile = load_profile.parse("step-up x10")
    assert [p.start_rate for p in profile.phases] == [1000, 2000, 4000]
    assert profile.name == "step-upx10"
    assert load_profile.parse(profile.spec).phases == profile.phases
    paused = load_profile.parse("10:5,0:5,10:5")
    assert len(list(paused.arrivals())) == 100
    assert not [t for t in paused.arrivals() if 5 <= t < 10]


@pytest.mark.parametrize("text", ["", "spike x0", "150", "150:0", "abc:10", "0:10", "150:60,"])
def test_invalid_profiles_are_rejected(text):
    with pytest.raises(ValueError):
        load_profile.parse(text)


def rows_for(profile, *, failing=(), slow=()):
    rows = []
    for index, t in enumerate(profile.arrivals()):
        code = "UNAVAILABLE" if any(a <= t < b for a, b in failing) else "OK"
        latency = 200.0 if any(a <= t < b for a, b in slow) else 20.0
        rows.append(
            {
                "index": index, "scheduled_s": t, "started_s": t, "schedule_lag_ms": 0.5, "code": code,
                "latency_ms": latency, "completed_s": t + latency / 1000, "instances": ["a-1", "b-2"],
            }
        )  # fmt: skip
    return rows


def test_phase_summary_reports_recovery_after_a_spike():
    profile = load_profile.parse("10:20,40:10,10:30")
    rows = rows_for(profile, failing=[(21, 24)], slow=[(30, 38)])
    first, spike, after = load_profile.phase_summary(rows, profile)
    assert first["goodput_rps"] == 10 and first["settle_s"] is None and first["p95_ms"] == 20
    assert spike["error_percent"] == pytest.approx(30)
    assert spike["settle_s"] == 4  # errors end at 24 s: the first clean 5 s window starts there
    assert after["settle_s"] == 8  # slow until 38 s, i.e. 8 s into the last phase
    assert after["share_b2_percent"] == 100
    series = load_profile.timeseries(rows, profile.duration)
    assert len(series) == 60 and series[23]["failed"] == 40 and series[0]["offered"] == 10


def test_phase_that_never_settles_reports_none():
    profile = load_profile.parse("10:10,40:10")
    rows = rows_for(profile, failing=[(10, 20)])
    assert load_profile.phase_summary(rows, profile)[1]["settle_s"] is None


def test_run_load_reports_changing_load_per_phase(tmp_path):
    profile = load_profile.parse("10:20,40:10,10:30")
    for rep in (1, 2):
        stem = f"even-xds-profile-{profile.name}-r{rep}"
        rows = rows_for(profile, failing=[(20, 20 + rep)])
        (tmp_path / f"{stem}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (tmp_path / f"{stem}.json").write_text(json.dumps({"parameters": {"duration": profile.duration}}))
        meta = {
            "variant": "xds", "scenario": "even", "repetition": rep, "rate": profile.mean_rate,
            "profile": profile.name, "profile_spec": profile.spec,
        }  # fmt: skip
        (tmp_path / f"{stem}.meta.json").write_text(json.dumps(meta))
    # analyse() alone writes the per-second CSV and keeps no rows: a long suite on a 2 GiB VM
    # ran out of memory when every trace's rows stayed alive until the report was built.
    single = run_load.analyse(tmp_path / f"even-xds-profile-{profile.name}-r1.json")
    assert not any(isinstance(value, list) and len(value) > 10 for value in single.values())
    assert (tmp_path / f"even-xds-profile-{profile.name}-r1.timeseries.csv").exists()
    text = run_load.report(tmp_path)
    assert "## Tải thay đổi theo đoạn" in text
    assert "| 2 | 40 RPS × 10 s |" in text
    assert "1.5 [1.0–2.0]" in text  # settle times of the two repetitions
    assert (tmp_path / f"even-xds-profile-{profile.name}-r1.timeseries.csv").read_text().startswith("second,")
    saved = json.loads((tmp_path / "report.json").read_text())
    assert all("_rows" not in run for run in saved) and len(saved[0]["phases"]) == 3


def test_run_load_main_records_profiles_in_run_json(tmp_path, monkeypatch):
    """The command line must get past writing run.json (it used to crash on Profile objects)."""
    import contextlib

    class FakeLab:
        address = "127.0.0.1:1"

        def provenance(self):
            return {"driver": "fake"}

        def keepalive(self):
            return contextlib.nullcontext()

        def lock(self, purpose):
            return contextlib.nullcontext()

    seen = {}

    async def fake_suite(args, lab, directory):
        seen["loads"] = args.loads

    monkeypatch.setattr(run_load, "make_lab", lambda **kwargs: FakeLab())
    monkeypatch.setattr(run_load, "suite", fake_suite)
    output = tmp_path / "run"
    argv = ["run_load.py", "--profiles", "low", "spike x2", "150:60,600:10", "--output", str(output)]
    monkeypatch.setattr(sys, "argv", argv)
    run_load.main()
    parameters = json.loads((output / "run.json").read_text(encoding="utf-8"))["parameters"]
    assert [load["name"] for load in parameters["loads"]] == ["low", "spikex2", "150-60-600-10"]
    assert parameters["loads"][1]["spec"] == "150:60,600:10,150:90 x2"
    assert parameters["output"] == str(output)
    assert [p.name for p in seen["loads"]] == ["low", "spikex2", "150-60-600-10"]
    # Constant rates still serialize as plain numbers.
    monkeypatch.setattr(sys, "argv", ["run_load.py", "--rates", "50", "100", "--output", str(tmp_path / "r")])
    run_load.main()
    assert json.loads((tmp_path / "r/run.json").read_text())["parameters"]["loads"] == [50.0, 100.0]


async def test_scale_probe_runs_a_profile_and_reports_each_phase(tmp_path):
    config = ServiceConfig("gateway", {"gateway-1": "gateway:50051"}, delay_s=0)
    runtime = await start_service(config, "gateway-1", {"gateway": config}, "127.0.0.1:0", request_log="off")
    profile = load_profile.parse("20:1,60:1")
    options = scale_probe_args(targets=[f"127.0.0.1:{runtime.port}"])
    try:
        result, raw = await asyncio.to_thread(scale_probe.run_step, options, profile, 0, "test")
    finally:
        await runtime.close()
    assert result["worker_errors"] == [] and result["passed"], result
    assert [p["goodput_rps"] for p in result["phases"]] == [20, 60]
    assert result["successful"] == 80
    scale_probe.save_raw(tmp_path, 0, result["rate"], options.workers, raw, profile)
    rate, workers, records, spec = scale_probe.load_raw(tmp_path, 0)
    again = scale_probe.summarize_step(rate, profile.duration, workers, records, load_profile.parse(spec))
    assert [p["latency_ms"] for p in again["phases"]] == [p["latency_ms"] for p in result["phases"]]


async def test_controlled_probe_follows_a_profile(tmp_path):
    from types import SimpleNamespace

    from controlled_probe import Probe

    config = ServiceConfig("service-a", {"a-1": "a:50051"}, delay_s=0)
    runtime = await start_service(config, "a-1", {"service-a": config}, "127.0.0.1:0", request_log="off")
    profile = load_profile.parse("20:1,40:1")
    output = tmp_path / "trace.json"
    args = SimpleNamespace(
        address=f"127.0.0.1:{runtime.port}", output=output, mode="lab", entry="service-a", workload="light",
        rate=profile.mean_rate, duration=profile.duration, timeout=2, ready_timeout=10, max_inflight=64,
        warmup=2, fault_at=None, stable_window=1, profile=profile,
    )  # fmt: skip
    try:
        await Probe(args).run()
    finally:
        await runtime.close()
    rows = [json.loads(line) for line in output.with_suffix(".jsonl").read_text().splitlines()]
    assert len(rows) == 60 and all(r["code"] == "OK" for r in rows)
    assert [p["goodput_rps"] for p in load_profile.phase_summary(rows, profile)] == [20, 40]
    assert json.loads(output.read_text())["parameters"]["profile"] == "20:1,40:1"


def scale_probe_args(**changes):
    from types import SimpleNamespace

    values = dict(
        targets=[], workers=2, channels=1, warmup=0.5, duration=1, timeout=2, ready_timeout=10,
        max_inflight=256, entry="gateway", workload="light", start_at=None,
    )  # fmt: skip
    values.update(changes)
    return SimpleNamespace(**values)
