"""Capacity load generator: real multi-process run, verdicts, and merging several machines."""

import asyncio
import sys
from array import array
from pathlib import Path
from types import SimpleNamespace

import pytest

from grpc_lb.service_runtime import start_service
from grpc_lb.topology import ServiceConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import scale_probe  # noqa: E402


def args(**changes):
    values = dict(
        targets=[], workers=2, channels=2, warmup=0.5, duration=1.5, timeout=2, ready_timeout=10,
        max_inflight=256, entry="gateway", workload="light", start_at=None,
    )  # fmt: skip
    values.update(changes)
    return SimpleNamespace(**values)


async def test_real_run_counts_instances_and_passes():
    config = ServiceConfig("gateway", {"gateway-1": "gateway:50051"}, delay_s=0)
    runtime = await start_service(config, "gateway-1", {"gateway": config}, "127.0.0.1:0", request_log="off")
    try:
        options = args(targets=[f"127.0.0.1:{runtime.port}"])
        result, raw = await asyncio.to_thread(scale_probe.run_step, options, 40, 0, "test")
    finally:
        await runtime.close()
    assert result["worker_errors"] == []
    assert result["valid_load"] and result["passed"], result
    assert result["successful"] == 60  # 40 RPS x 1.5 s, warmup excluded
    assert result["instances"] == {"gateway": {"gateway-1": 60}}
    assert len(raw) == 2


def worker(latencies, codes, *, lag=1.0, error=None):
    if error:
        return {"worker": 0, "error": error}
    return {
        "worker": 0,
        "latencies": array("f", latencies).tobytes(),
        "lags": array("f", [lag] * sum(codes.values())).tobytes(),
        "codes": codes,
        "instances": {"a-1": codes.get("OK", 0)},
        "per_second": [[0, 1, codes.get("OK", 0)]],
        "overflow": codes.get("GENERATOR_OVERFLOW", 0),
    }


def test_verdicts_separate_cluster_failure_from_generator_failure():
    ok = scale_probe.summarize(10, 1, 1, [worker([5.0] * 10, {"OK": 10})])
    assert ok["valid_load"] and ok["passed"]
    errors = scale_probe.summarize(10, 1, 1, [worker([5.0] * 8, {"OK": 8, "DEADLINE_EXCEEDED": 2})])
    assert errors["valid_load"] and not errors["passed"] and errors["error_percent"] == pytest.approx(20)
    lagging = scale_probe.summarize(10, 1, 1, [worker([5.0] * 10, {"OK": 10}, lag=500)])
    assert not lagging["valid_load"] and not lagging["passed"]
    crashed = scale_probe.summarize(10, 1, 2, [worker([5.0] * 10, {"OK": 10}), worker([], {}, error="boom")])
    assert not crashed["valid_load"] and crashed["worker_errors"] == ["boom"]


def test_merge_adds_rates_and_recomputes_percentiles(tmp_path):
    for name, latency in (("m1", 10.0), ("m2", 30.0)):
        directory = tmp_path / name
        directory.mkdir()
        scale_probe.save_raw(directory, 0, 5, 1, [worker([latency] * 5, {"OK": 5})])
        report = {
            "host": name, "targets": [f"{name}:80"], "workers": 1, "warmup": 0, "duration": 1,
            "channels": 1, "steps": [{}],
        }  # fmt: skip
        (directory / "report.json").write_text(scale_probe.json.dumps(report))
    merged = scale_probe.merge([tmp_path / "m1", tmp_path / "m2"], tmp_path / "all")
    step = merged["steps"][0]
    assert step["rate"] == 10 and step["successful"] == 10 and step["passed"]
    assert step["latency_ms"]["p50"] == 10.0 and step["latency_ms"]["p99"] == 30.0
    assert step["instances"] == {"a": {"a-1": 10}}
    assert "| 10 | 600 |" in (tmp_path / "all/report.md").read_text(encoding="utf-8")
