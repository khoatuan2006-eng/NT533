from __future__ import annotations

import json
from pathlib import Path
import pytest

from grpc_lb.contracts import CallResult, CallStatus
from grpc_lb.telemetry import TelemetryWriter, read_telemetry, result_to_dict


def test_result_to_dict_preserves_all_fields() -> None:
    res = CallResult(
        run_id="run-001",
        request_id="req-123",
        policy="round_robin",
        backend_id="replica-1",
        started_at_unix_ns=1700000000000000000,
        latency_ms=12.5,
        status=CallStatus.OK,
        grpc_code=None,
        error_message=None,
        schema_version=1,
    )

    d = result_to_dict(res)
    assert d == {
        "run_id": "run-001",
        "request_id": "req-123",
        "policy": "round_robin",
        "backend_id": "replica-1",
        "started_at_unix_ns": 1700000000000000000,
        "latency_ms": 12.5,
        "status": "ok",
        "grpc_code": None,
        "error_message": None,
        "schema_version": 1,
    }


def test_telemetry_writer_writes_valid_jsonl(tmp_path: Path) -> None:
    log_file = tmp_path / "subdir" / "test_metrics.jsonl"
    writer = TelemetryWriter(log_file)

    res1 = CallResult(
        run_id="run-test",
        request_id="req-1",
        policy="round_robin",
        backend_id="replica-1",
        started_at_unix_ns=1000,
        latency_ms=5.0,
        status=CallStatus.OK,
    )
    res2 = CallResult(
        run_id="run-test",
        request_id="req-2",
        policy="round_robin",
        backend_id="replica-2",
        started_at_unix_ns=2000,
        latency_ms=105.0,
        status=CallStatus.TIMEOUT,
        grpc_code="DEADLINE_EXCEEDED",
        error_message="Deadline exceeded",
    )
    res3 = CallResult(
        run_id="run-test",
        request_id="req-3",
        policy="round_robin",
        backend_id=None,
        started_at_unix_ns=3000,
        latency_ms=0.5,
        status=CallStatus.NO_BACKEND,
        error_message="No healthy backend available",
    )

    writer.record(res1)
    writer.record_many([res2, res3])
    writer.flush()
    writer.close()

    assert writer.total_recorded == 3
    assert log_file.exists()

    records = read_telemetry(log_file)
    assert len(records) == 3
    assert records[0]["request_id"] == "req-1"
    assert records[0]["status"] == "ok"
    assert records[1]["request_id"] == "req-2"
    assert records[1]["status"] == "timeout"
    assert records[1]["grpc_code"] == "DEADLINE_EXCEEDED"
    assert records[2]["request_id"] == "req-3"
    assert records[2]["status"] == "no_backend"
    assert records[2]["backend_id"] is None


def test_telemetry_writer_context_manager(tmp_path: Path) -> None:
    log_file = tmp_path / "cm_test.jsonl"

    res = CallResult(
        run_id="cm-run",
        request_id="cm-req",
        policy="least_request",
        backend_id="replica-3",
        started_at_unix_ns=5000,
        latency_ms=2.0,
        status=CallStatus.OK,
    )

    with TelemetryWriter(log_file) as writer:
        writer.record(res)
        assert writer.total_recorded == 1

    # Verify that file was closed and data was flushed
    records = read_telemetry(log_file)
    assert len(records) == 1
    assert records[0]["policy"] == "least_request"


# ---------------------------------------------------------------------------
# Unit tests for Summarize module
# ---------------------------------------------------------------------------

from grpc_lb.summarize import (
    AggregatedSummary,
    RunSummary,
    aggregate_runs,
    compute_percentiles,
    format_summary_table,
    summarize_file,
    summarize_records,
)


def test_compute_percentiles_accuracy() -> None:
    # Với dãy số từ 1 đến 100
    values = list(range(1, 101))
    pcts = compute_percentiles(values, [50.0, 95.0, 99.0])

    # Rank của p50 là 0.5 * 99 = 49.5 -> nội suy giữa 50 và 51 -> 50.5
    assert pcts[50.0] == 50.5
    # Rank của p95 là 0.95 * 99 = 94.05 -> nội suy giữa 95 và 96 -> 95.05
    assert pcts[95.0] == 95.05
    # Rank của p99 là 0.99 * 99 = 98.01 -> nội suy giữa 99 và 100 -> 99.01
    assert pcts[99.0] == 99.01


def test_compute_percentiles_empty_and_single() -> None:
    assert compute_percentiles([]) == {50.0: 0.0, 95.0: 0.0, 99.0: 0.0}
    assert compute_percentiles([42.0]) == {50.0: 42.0, 95.0: 42.0, 99.0: 42.0}


def test_summarize_records_metrics() -> None:
    # Tạo 10 bản ghi: 8 ok, 1 timeout, 1 no_backend
    records = [
        {
            "run_id": "test-run",
            "request_id": f"req-{i}",
            "policy": "round_robin",
            "backend_id": f"replica-{(i % 3) + 1}",
            "started_at_unix_ns": 1_000_000_000 + i * 100_000_000,  # 0.1s mỗi req -> 0.9s duration
            "latency_ms": float(10 + i * 5),
            "status": "ok",
        }
        for i in range(8)
    ]
    records.append(
        {
            "run_id": "test-run",
            "request_id": "req-8",
            "policy": "round_robin",
            "backend_id": "replica-2",
            "started_at_unix_ns": 1_800_000_000,
            "latency_ms": 2000.0,
            "status": "timeout",
        }
    )
    records.append(
        {
            "run_id": "test-run",
            "request_id": "req-9",
            "policy": "round_robin",
            "backend_id": None,
            "started_at_unix_ns": 1_900_000_000,
            "latency_ms": 0.5,
            "status": "no_backend",
        }
    )

    summary = summarize_records(records)

    assert summary.total_requests == 10
    assert summary.ok_count == 8
    assert summary.timeout_count == 1
    assert summary.no_backend_count == 1
    assert summary.ok_rate_pct == 80.0
    assert summary.timeout_rate_pct == 10.0
    assert summary.error_rate_pct == 10.0
    assert summary.duration_s == 0.9
    assert summary.throughput_rps == round(8 / 0.9, 2)
    assert summary.p50_latency_ms > 0
    assert summary.backend_distribution["replica-1"] == 3
    assert summary.backend_distribution["replica-2"] == 4
    assert summary.backend_distribution["replica-3"] == 2
    assert summary.backend_distribution["none"] == 1


def test_summarize_file_and_format_table(tmp_path: Path) -> None:
    log_file = tmp_path / "run_table.jsonl"
    with TelemetryWriter(log_file) as writer:
        for i in range(5):
            writer.record(
                CallResult(
                    run_id="run-table",
                    request_id=f"r-{i}",
                    policy="least_request",
                    backend_id="replica-1",
                    started_at_unix_ns=1_000_000_000 + i * 200_000_000,
                    latency_ms=10.0 + i,
                    status=CallStatus.OK,
                )
            )

    summary = summarize_file(log_file)
    assert summary.total_requests == 5
    assert summary.ok_count == 5

    table_text = format_summary_table(summary)
    assert "RUN SUMMARY: run-table" in table_text
    assert "Policy: least_request" in table_text
    assert "Throughput" in table_text
    assert "replica-1" in table_text


def test_aggregate_runs() -> None:
    s1 = RunSummary(
        run_id="run-1",
        policy="round_robin",
        total_requests=100,
        ok_count=98,
        timeout_count=2,
        error_count=0,
        no_backend_count=0,
        ok_rate_pct=98.0,
        timeout_rate_pct=2.0,
        error_rate_pct=0.0,
        duration_s=10.0,
        throughput_rps=9.8,
        p50_latency_ms=10.0,
        p95_latency_ms=20.0,
        p99_latency_ms=30.0,
        mean_latency_ms=11.0,
        min_latency_ms=5.0,
        max_latency_ms=35.0,
        backend_distribution={"replica-1": 50, "replica-2": 50},
        backend_distribution_pct={"replica-1": 50.0, "replica-2": 50.0},
    )
    s2 = RunSummary(
        run_id="run-2",
        policy="round_robin",
        total_requests=100,
        ok_count=100,
        timeout_count=0,
        error_count=0,
        no_backend_count=0,
        ok_rate_pct=100.0,
        timeout_rate_pct=0.0,
        error_rate_pct=0.0,
        duration_s=10.0,
        throughput_rps=10.0,
        p50_latency_ms=12.0,
        p95_latency_ms=22.0,
        p99_latency_ms=32.0,
        mean_latency_ms=12.0,
        min_latency_ms=6.0,
        max_latency_ms=36.0,
        backend_distribution={"replica-1": 50, "replica-2": 50},
        backend_distribution_pct={"replica-1": 50.0, "replica-2": 50.0},
    )

    agg = aggregate_runs([s1, s2])
    assert agg.runs_count == 2
    assert agg.policy == "round_robin"
    assert agg.mean_throughput_rps == 9.9
    assert agg.mean_p50_latency_ms == 11.0
    assert agg.mean_ok_rate_pct == 99.0
    assert agg.aggregated_backend_distribution == {"replica-1": 100, "replica-2": 100}


# ---------------------------------------------------------------------------
# Unit tests for Load Generator module
# ---------------------------------------------------------------------------

from grpc_lb.client import WorkloadClient
from grpc_lb.loadgen import LoadGenReport, WorkloadConfig, WorkloadGenerator
from test_client import LocalTestCluster


def test_workload_config_parsing() -> None:
    cfg = WorkloadConfig.from_file("configs/low.json")
    assert cfg.name == "low"
    assert cfg.rps == 30.0
    assert cfg.duration_s == 60.0
    assert cfg.warmup_s == 10.0
    assert cfg.max_in_flight == 100
    assert cfg.timeout_s == 2.0
    assert cfg.repetitions == 3
    assert cfg.seed == 533


@pytest.mark.asyncio
async def test_loadgen_execution_and_telemetry(tmp_path: Path) -> None:
    cluster = LocalTestCluster()
    try:
        await cluster.add_server("replica-1")
        await cluster.add_server("replica-2")

        log_file = tmp_path / "loadgen_test.jsonl"
        with TelemetryWriter(log_file) as telemetry:
            async with WorkloadClient(
                backends=cluster.backend_configs,
                policy="round_robin",
            ) as client:
                loadgen = WorkloadGenerator(client, telemetry=telemetry)
                # 50 RPS trong 0.2s -> 10 requests, warmup 0.1s -> 5 requests
                cfg = WorkloadConfig(
                    name="test",
                    rps=50.0,
                    duration_s=0.2,
                    warmup_s=0.1,
                    max_in_flight=50,
                    timeout_s=1.0,
                )
                report = await loadgen.run(cfg, run_id="run-lg-test")

                assert report.total_scheduled == 10
                assert report.total_dispatched == 10
                assert report.total_dropped == 0
                assert report.total_completed == 10
                assert report.actual_rps > 0
                assert report.warmup_requests == 5

        # Xác nhận đúng 10 bản ghi trong file log (warmup không được ghi vào file)
        records = read_telemetry(log_file)
        assert len(records) == 10
        for r in records:
            assert r["run_id"] == "run-lg-test"
            assert r["status"] == "ok"
    finally:
        await cluster.shutdown()


@pytest.mark.asyncio
async def test_loadgen_concurrency_saturation_drop() -> None:
    cluster = LocalTestCluster()
    try:
        # Server có delay 0.2s để gây nghẽn hàng đợi
        await cluster.add_server("replica-slow", delay_s=0.2)

        async with WorkloadClient(
            backends=cluster.backend_configs,
            policy="round_robin",
        ) as client:
            loadgen = WorkloadGenerator(client, telemetry=None)
            # 100 RPS trong 0.1s -> 10 requests, nhưng max_in_flight chỉ bằng 1
            cfg = WorkloadConfig(
                name="test-saturated",
                rps=100.0,
                duration_s=0.1,
                warmup_s=0.0,
                max_in_flight=1,
                timeout_s=1.0,
            )
            report = await loadgen.run(cfg, run_id="run-saturated")

            assert report.total_scheduled == 10
            # Khi max_in_flight=1 và server trễ 0.2s, các request sau sẽ bị drop
            assert report.total_dropped > 0
            assert report.total_dispatched + report.total_dropped == 10
            assert report.total_completed == report.total_dispatched
    finally:
        await cluster.shutdown()


