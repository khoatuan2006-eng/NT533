"""Prometheus metrics export for gRPC Service A -> Service B traffic observability."""

from __future__ import annotations

import logging
import threading
from typing import Any

LOG = logging.getLogger(__name__)

# Fallback dummy class if prometheus_client is not installed
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    HAVE_PROMETHEUS = True
except ImportError:
    HAVE_PROMETHEUS = False

    class _DummyMetric:
        def labels(self, *args: Any, **kwargs: Any) -> "_DummyMetric":
            return self

        def inc(self, amount: float = 1) -> None:
            pass

        def observe(self, amount: float) -> None:
            pass

        def set(self, value: float) -> None:
            pass

        def clear(self) -> None:
            pass

    Counter = _DummyMetric  # type: ignore[misc, assignment]
    Gauge = _DummyMetric  # type: ignore[misc, assignment]
    Histogram = _DummyMetric  # type: ignore[misc, assignment]

    def start_http_server(port: int, addr: str = "0.0.0.0") -> None:  # type: ignore[misc]
        LOG.warning("prometheus_client not installed, cannot start metrics server on port %s", port)


_SERVER_STARTED = False
_LOCK = threading.Lock()

if HAVE_PROMETHEUS:
    CALLS_SENT = Counter(
        "grpc_client_calls_sent_total",
        "Total number of gRPC requests sent from upstream service to downstream dependency",
        ["caller", "target", "status"],
    )
    CALL_DURATION = Histogram(
        "grpc_client_call_duration_seconds",
        "Duration of gRPC requests sent to downstream dependency in seconds",
        ["caller", "target"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    )
    # Weight controller in the control plane (xds-beta / xds-rl): what it decided for A -> B.
    RL_BETA = Gauge(
        "grpc_lb_rl_beta",
        "Beta the weight controller is applying (0 = Round Robin, larger = moves more load off slow replicas)",
        ["service", "policy"],
    )
    RL_WEIGHT = Gauge(
        "grpc_lb_rl_weight_percent",
        "Weight the controller gives each endpoint; the weights of a service sum to 100",
        ["service", "endpoint", "replica"],
    )
    RL_FALLBACK = Gauge(
        "grpc_lb_rl_fallback",
        "1 while the controller has fallen back to Round Robin because its telemetry is not usable",
        ["service", "policy"],
    )
else:
    CALLS_SENT = Counter()
    CALL_DURATION = Histogram()
    RL_BETA = Gauge()
    RL_WEIGHT = Gauge()
    RL_FALLBACK = Gauge()

# Instance n of a service listens on 50100/50200/50300 + n (see scripts/multihost.py).
_PORT_TIERS = {501: "a", 502: "b", 503: "c"}


def replica_name(endpoint: str) -> str:
    """b-2 for 172.31.30.73:50202; the endpoint itself when the port follows no known layout."""
    _, _, port = endpoint.rpartition(":")
    if port.isdigit() and int(port) // 100 in _PORT_TIERS and int(port) % 100:
        return f"{_PORT_TIERS[int(port) // 100]}-{int(port) % 100}"
    return endpoint


def record_rl_window(service: str, policy: str, beta: float, weights: dict[str, float]) -> None:
    """Publish one controller decision: its beta and the weight of every endpoint."""
    try:
        RL_BETA.labels(service=service, policy=policy).set(beta)
        RL_FALLBACK.labels(service=service, policy=policy).set(0)
        for endpoint, weight in weights.items():
            RL_WEIGHT.labels(service=service, endpoint=endpoint, replica=replica_name(endpoint)).set(weight)
    except Exception as exc:
        LOG.debug("Failed to record controller metric: %s", exc)


def record_rl_fallback(service: str, policy: str) -> None:
    """The controller handed the service back to Round Robin: its weights no longer apply."""
    try:
        RL_WEIGHT.clear()
        RL_FALLBACK.labels(service=service, policy=policy).set(1)
    except Exception as exc:
        LOG.debug("Failed to record controller metric: %s", exc)


def record_call(
    caller: str,
    target: str,
    status: str = "ok",
    duration_s: float | None = None,
) -> None:
    """Record a completed or failed downstream dependency call."""
    try:
        CALLS_SENT.labels(caller=caller, target=target, status=status).inc(1)
        if duration_s is not None and duration_s >= 0:
            CALL_DURATION.labels(caller=caller, target=target).observe(duration_s)
    except Exception as exc:
        LOG.debug("Failed to record prometheus metric: %s", exc)


def init_metrics(caller: str, known_targets: list[str] | None = None) -> None:
    """Initialize metric series to 0 so they appear in Prometheus / Grafana immediately."""
    if not HAVE_PROMETHEUS:
        return
    targets = known_targets or ["b-1", "b-2", "b-3", "b-4"]
    for t in targets:
        try:
            CALLS_SENT.labels(caller=caller, target=t, status="ok").inc(0)
        except Exception:
            pass


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> bool:
    """Start the Prometheus HTTP metrics exporter server in a daemon thread."""
    global _SERVER_STARTED
    if not HAVE_PROMETHEUS:
        LOG.warning("Cannot start metrics server: prometheus_client is not installed")
        return False
    with _LOCK:
        if _SERVER_STARTED:
            return True
        try:
            start_http_server(port, addr=addr)
            _SERVER_STARTED = True
            LOG.info("Prometheus metrics server started on %s:%d", addr, port)
            return True
        except Exception as exc:
            LOG.error("Failed to start Prometheus metrics server on %s:%d: %s", addr, port, exc)
            return False
