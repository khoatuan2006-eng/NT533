from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CallStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    NO_BACKEND = "no_backend"


@dataclass(frozen=True, slots=True)
class CallResult:
    run_id: str
    request_id: str
    policy: str
    backend_id: str | None
    started_at_unix_ns: int
    latency_ms: float
    status: CallStatus
    grpc_code: str | None = None
    error_message: str | None = None
    schema_version: int = 1
