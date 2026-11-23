"""Weight-controller metrics: what the control plane exports, how it is deployed and shown."""

import json
import runpy
import time
from pathlib import Path

from prometheus_client import REGISTRY
from test_rl_shape import _window

from grpc_lb import prom_metrics
from grpc_lb.controlplane.resources import ResourceBuilder
from grpc_lb.controlplane.rl_controller import ShapeController

ROOT = Path(__file__).resolve().parents[1]


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels)


def test_replica_name_follows_the_port_layout():
    assert prom_metrics.replica_name("172.31.30.73:50202") == "b-2"
    assert prom_metrics.replica_name("10.0.0.1:50112") == "a-12"
    assert prom_metrics.replica_name("10.0.0.1:50300") == "10.0.0.1:50300"
    assert prom_metrics.replica_name("10.0.0.1:8080") == "10.0.0.1:8080"


async def test_controller_exports_beta_weights_and_fallback(tmp_path):
    endpoints = ["127.0.0.1:50201", "127.0.0.1:50202"]
    base = await ResourceBuilder().build({"metrics-svc": endpoints})
    path = tmp_path / "telemetry.json"
    controller = ShapeController(str(path), "metrics-svc", policy="shape-fixed", beta=2)
    for sequence in range(1, 4):
        path.write_text(json.dumps({**_window(endpoints, [10, 60], sequence), "service": "metrics-svc"}))
        controller.apply(base)
    assert sample("grpc_lb_rl_beta", service="metrics-svc", policy="shape-fixed") == 2
    assert sample("grpc_lb_rl_fallback", service="metrics-svc", policy="shape-fixed") == 0
    weights = [
        sample("grpc_lb_rl_weight_percent", service="metrics-svc", endpoint=e, replica=r)
        for e, r in zip(endpoints, ("b-1", "b-2"), strict=True)
    ]
    assert sum(weights) == 100 and weights[0] > weights[1]
    stale = {**_window(endpoints, [10, 60], 9), "service": "metrics-svc", "timestamp": time.time() - 100}
    path.write_text(json.dumps(stale))
    controller.apply(base)
    assert sample("grpc_lb_rl_fallback", service="metrics-svc", policy="shape-fixed") == 1
    assert (
        sample("grpc_lb_rl_weight_percent", service="metrics-svc", endpoint=endpoints[0], replica="b-1")
        is None
    )


def test_control_planes_get_a_metrics_port_and_prometheus_scrapes_it(tmp_path):
    module = runpy.run_path(str(ROOT / "scripts/multihost.py"))
    spec = module["ClusterSpec"].three_nodes(["192.168.1.201", "192.168.1.202", "192.168.1.203"])
    module["ClusterDeployment"](spec).render(tmp_path)
    plane = json.loads((tmp_path / "node1/compose.json").read_text())["services"]["controlplane"]
    assert plane["environment"]["METRICS_PORT"] == "9201"
    labels = [label for label, _, _ in module["connectivity_checks"](spec)["node1"]]
    assert "metrics-controlplane node1" in labels
    config = (ROOT / "deploy/monitoring/prometheus/prometheus.node4.yml").read_text(encoding="utf-8")
    job = config[config.index("job_name: controlplane-metrics") :]
    assert '"172.31.20.204:9201"' in job and '"172.31.30.73:9201"' in job


def test_dashboard_shows_the_controller_row():
    dashboard = json.loads(
        (ROOT / "deploy/monitoring/grafana/dashboards/grpc-lb-overview.json").read_text(encoding="utf-8")
    )
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    exprs = " ".join(t["expr"] for p in dashboard["panels"] for t in p.get("targets", []))
    for name in ("grpc_lb_rl_beta", "grpc_lb_rl_fallback", "grpc_lb_rl_weight_percent"):
        assert name in exprs


def test_share_panels_compute_real_percentages():
    """A 100%-stacked rate fakes a share: unstacked or in the legend it prints req/s x 100."""
    dashboard = json.loads(
        (ROOT / "deploy/monitoring/grafana/dashboards/grpc-lb-overview.json").read_text(encoding="utf-8")
    )
    panels = {panel["id"]: panel for panel in dashboard["panels"]}
    for panel_id in (11, 401):
        panel = panels[panel_id]
        assert "scalar(sum(" in panel["targets"][0]["expr"]
        assert panel["fieldConfig"]["defaults"]["custom"]["stacking"]["mode"] == "none"
        assert panel["fieldConfig"]["defaults"]["unit"] == "percentunit"
    assert 'target=~"b-.*"' in panels[401]["targets"][0]["expr"]
