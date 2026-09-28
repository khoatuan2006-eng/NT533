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
