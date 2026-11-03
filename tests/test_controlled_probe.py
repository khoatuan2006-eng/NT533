"""Measurement correctness: errors/dropped arrivals cannot improve the verdict."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from grpc_lb.microservices import start_service
from grpc_lb.topology import ServiceConfig

spec = importlib.util.spec_from_file_location(
    "controlled_probe", Path(__file__).resolve().parents[1] / "scripts/controlled_probe.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def row(at, code="OK", latency=10):
    return {"scheduled_s": at, "code": code, "latency_ms": latency, "schedule_lag_ms": 0}


def test_errors_and_generator_drops_are_visible_in_denominator():
    result = probe.summarize(
        [row(0), row(1, "UNAVAILABLE", 1000), {"scheduled_s": 2, "code": "GENERATOR_OVERFLOW"}], 3
    )
    assert result["successful_rps_offered_window"] == 1 / 3
    assert result["rpc_error_fraction"] == 0.5
    assert result["failure_fraction_including_generator_drops"] == 1 - 1 / 3
    assert result["success_latency_ms"]["p99"] == 10
    assert result["all_rpc_latency_ms"]["p99"] == 1000
    assert not result["valid_load"]


def test_recovery_requires_full_stable_window_after_last_failure():
    rows = [row(i, "UNAVAILABLE" if i in (2, 4) else "OK") for i in range(10)]
    assert probe.summarize(rows, 10, fault_at=2, stable_window=3)["observed_recovery_s"] == 3
    assert probe.summarize(rows[:6], 6, fault_at=2, stable_window=3)["observed_recovery_s"] is None


def test_no_samples_are_not_zero_latency_or_zero_error():
    result = probe.summarize([], 10)
    assert result["success_latency_ms"]["p95"] is None
    assert result["rpc_error_fraction"] is None
    assert result["observed_recovery_s"] is None


def test_recovery_window_cannot_precede_confirmed_fault():
    rows = [row(i) for i in range(12)]
    assert probe.summarize(rows, 12, 2, 3, search_after=6)["observed_recovery_s"] == 4


def test_interruption_uses_last_failure_and_requires_a_complete_window():
    rows = [row(i, "UNAVAILABLE" if i in (3, 8) else "OK") for i in range(16)]
    assert probe.interruption(rows, 2, 15)["rpc_interruption_s"] == 6
    assert probe.interruption(rows, 2, 12)["rpc_interruption_s"] is None
    assert probe.interruption([row(i) for i in range(10)], 2, 9)["rpc_interruption_s"] == 0


@pytest.mark.asyncio
async def test_real_probe_records_every_arrival_without_early_dispatch(tmp_path):
    config = ServiceConfig("leaf", {"leaf-1": "127.0.0.1:50051"}, delay_s=0)
    node = await start_service(config, "leaf-1", {"leaf": config}, "127.0.0.1:0")
    output = tmp_path / "run.json"
    args = SimpleNamespace(
        address=f"127.0.0.1:{node.port}",
        output=output,
        mode="static",
        entry="leaf",
        workload="light",
        rate=50,
        duration=0.1,
        timeout=1,
        ready_timeout=2,
        max_inflight=8,
        warmup=1,
        fault_at=None,
        stable_window=5,
    )
    try:
        await probe.Probe(args).run()
        rows = [json.loads(line) for line in output.with_suffix(".jsonl").read_text().splitlines()]
        assert len(rows) == 5
        assert all(r["started_s"] >= r["scheduled_s"] and r["code"] == "OK" for r in rows)
        assert json.loads(output.read_text())["summary"]["successful"] == 5
    finally:
        await node.close()
