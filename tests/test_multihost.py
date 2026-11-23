"""Validate VM placement and release boundaries before touching cloud machines."""

import json
import runpy
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from grpc_lb.topology import load_topology

MODULE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/multihost.py"))
ClusterDeployment = MODULE["ClusterDeployment"]


def test_shared_hosts_can_avoid_k3s_ingress_port(tmp_path):
    ClusterDeployment(["192.168.1.238", "192.168.1.172", "192.168.1.217"], 18080).render(tmp_path)
    for node in ("node1", "node2"):
        services = json.loads((tmp_path / node / "compose.json").read_text())["services"]
        assert services["ingress"]["ports"][0] == "18080:50050"
    with pytest.raises(ValueError):
        ClusterDeployment(["192.168.1.238", "192.168.1.172", "192.168.1.217"], 65536)


@pytest.mark.parametrize(
    "addresses",
    [
        ["192.168.1.1"] * 3,
        ["127.0.0.1", "192.168.1.2", "192.168.1.3"],
        ["0.0.0.0", "192.168.1.2", "192.168.1.3"],
        ["8.8.8.8", "192.168.1.2", "192.168.1.3"],
    ],
)
def test_invalid_cluster_addresses(addresses):
    with pytest.raises(ValueError):
        ClusterDeployment(addresses)


def test_three_node_placement_and_network_addresses(tmp_path):
    ClusterDeployment(["192.168.1.201", "192.168.1.202", "192.168.1.203"]).render(tmp_path)
    _, topology = load_topology(tmp_path / "topology.json")
    assert topology["service-b"].instances == {
        "b-1": "192.168.1.201:50201",
        "b-2": "192.168.1.203:50202",
        "b-3": "192.168.1.202:50203",
    }
    bootstrap = json.loads((tmp_path / "bootstrap.json").read_text())
    assert bootstrap["xds_servers"][0]["server_uri"] == "ipv4:192.168.1.201:18000,192.168.1.202:18000"
    for index in range(1, 4):
        compose = json.loads((tmp_path / f"node{index}/compose.json").read_text())
        services = compose["services"]
        assert services["consul"]["command"].count("-bootstrap-expect=3") == 1
        assert all(
            service["network_mode"] == "host" for name, service in services.items() if name != "ingress"
        )
        if "ingress" in services:
            # Envoy admin is published so Prometheus on node4 can scrape it; the security group keeps it in the VPC.
            assert services["ingress"]["ports"] == ["80:50050", "9901:9901"]
            assert "network_mode" not in services["ingress"]
        assert ("controlplane" in services) == (index < 3)
    envoy = (tmp_path / "node1/envoy.yaml").read_text()
    assert "address: 192.168.1.202, port_value: 50102" in envoy
    assert "port_value: 50050" in envoy


ClusterSpec = MODULE["ClusterSpec"]
EXAMPLE = Path(__file__).parents[1] / "configs/cluster.example.json"


def example_spec(tmp_path, **changes):
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for index, node in enumerate(raw["nodes"].values(), 1):
        node["ip"], node["ssh"] = f"10.0.1.{index}", f"203.0.113.{index}"
    raw.update(changes)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_controlplane_reads_every_consul_server_and_servers_listen_privately(tmp_path):
    ClusterDeployment(["192.168.1.201", "192.168.1.202", "192.168.1.203"]).render(tmp_path)
    node1 = json.loads((tmp_path / "node1/compose.json").read_text())["services"]
    assert node1["controlplane"]["environment"]["CONSUL_HTTP_ADDR"].split(",") == [
        "http://127.0.0.1:8500",
        "http://192.168.1.202:8500",
        "http://192.168.1.203:8500",
    ]
    assert "-client=127.0.0.1 192.168.1.201" in node1["consul"]["command"]
    assert all(service["logging"]["options"]["max-size"] for service in node1.values())


def test_example_spec_without_addresses_is_rejected():
    with pytest.raises(ValueError, match="IPv4"):
        ClusterSpec.from_file(EXAMPLE)


def test_scaled_spec_renders_every_tier(tmp_path):
    spec = ClusterSpec.from_file(example_spec(tmp_path))
    out = tmp_path / "cluster"
    ClusterDeployment(spec).render(out)
    _, topology = load_topology(out / "topology.json")
    assert list(topology["service-a"].instances) == ["a-1", "a-2", "a-3", "a-4", "a-5"]
    assert len(topology["service-b"].instances) == 5 and len(topology["service-c"].instances) == 4
    assert topology["service-a"].instances["a-3"] == "10.0.1.3:50103"
    bootstrap = json.loads((out / "bootstrap.json").read_text())
    assert bootstrap["xds_servers"][0]["server_uri"] == "ipv4:10.0.1.3:18000,10.0.1.4:18000"
    envoy = (out / "node1/envoy.yaml").read_text()
    assert envoy.count("- endpoint:") == 5 and "address: 10.0.1.5, port_value: 50105" in envoy
    assert "max_requests: 250" in envoy and "max_connections: 20" in envoy
    assert "grpc_status_filter" in envoy and "lb_policy: ROUND_ROBIN" in envoy
    for node in ("node1", "node6"):
        services = json.loads((out / f"{node}/compose.json").read_text())["services"]
        command = services["consul"]["command"]
        assert "-server" not in command and "-client=127.0.0.1" in command
        assert [c for c in command if c.startswith("-retry-join")] == [
            "-retry-join=10.0.1.3", "-retry-join=10.0.1.4", "-retry-join=10.0.1.5",
        ]  # fmt: skip
    servers = json.loads((out / "node5/compose.json").read_text())["services"]
    assert "-bootstrap-expect=3" in servers["consul"]["command"] and "controlplane" not in servers
    assert servers["a-5"]["environment"]["REQUEST_LOG"] == "errors"
    lab = json.loads((out / "lab.json").read_text())
    assert lab["address"] == "10.0.1.1:80"
    assert lab["nodes"]["node3"]["services"]["consul-1"] == "consul"
    assert lab["nodes"]["node1"]["services"]["envoy-1"] == "ingress"
    assert lab["nodes"]["node6"]["services"]["consul-agent-node6"] == "consul"


@pytest.mark.parametrize(
    "change",
    [
        {"consul_servers": ["node3", "node4"]},
        {"controlplanes": ["node9"]},
        {"ingress": []},
        {"envoy_access_log": "verbose"},
        {"unknown": 1},
        {"concurrency": {"service-a": 0}},
    ],
)
def test_invalid_specs_fail_before_rendering(tmp_path, change):
    with pytest.raises(ValueError):
        ClusterSpec.from_file(example_spec(tmp_path, **change))


def test_spec_rejects_missing_tier_and_duplicate_instance(tmp_path):
    raw = json.loads(example_spec(tmp_path).read_text())
    raw["nodes"]["node6"]["instances"] = ["a-1"]
    (tmp_path / "dup.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="twice"):
        ClusterSpec.from_file(tmp_path / "dup.json")
    raw = json.loads(example_spec(tmp_path).read_text())
    for node in raw["nodes"].values():
        node["instances"] = [i for i in (node["instances"] if isinstance(node["instances"], list) else [])]
    raw["nodes"]["node1"]["instances"] = ["a-1", "b-1"]
    (tmp_path / "no-c.json").write_text(json.dumps(raw))
    # C is optional (A then calls B only); A and B are not.
    assert ClusterSpec.from_file(tmp_path / "no-c.json").nodes["node1"].instances == ["a-1", "b-1"]
    raw["nodes"]["node1"]["instances"] = ["a-1"]
    (tmp_path / "no-b.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="service-b"):
        ClusterSpec.from_file(tmp_path / "no-b.json")


def test_connectivity_checks_cover_what_each_role_needs(tmp_path):
    spec = ClusterSpec.from_file(example_spec(tmp_path))
    checks = {
        node: {label for label, _, _ in items} for node, items in MODULE["connectivity_checks"](spec).items()
    }
    assert {"consul-http node5", "probe b-5", "xds node4"} <= checks["node3"]
    assert {"envoy->a-5", "consul-rpc node3"} <= checks["node1"]
    assert "consul-http node3" not in checks["node1"]  # only control planes read Consul over HTTP
    assert "call c-4" in checks["node1"]  # node1 runs a-1, which calls every C instance


def test_plan_sizes_a_million_rpm(tmp_path):
    result = MODULE["plan"](
        16667, {"service-a": 900, "service-b": 1400, "service-c": 1400}, 8,
        headroom=0.7, envoy_rps_per_core=4000, envoy_concurrency=4,
    )  # fmt: skip
    assert result["instances"] == {"service-a": 27, "service-b": 18, "service-c": 18}
    assert result["ingress_nodes"] == 2
    skeleton = MODULE["plan_spec"](result, 4)
    placed = [i for node in skeleton["nodes"].values() for i in node["instances"]]
    assert sorted(placed) == sorted(
        [f"{k}-{n}" for k, count in (("a", 27), ("b", 18), ("c", 18)) for n in range(1, count + 1)]
    )
    for node in skeleton["nodes"].values():
        node["ip"] = f"10.0.2.{len(placed) % 200 + list(skeleton['nodes'].values()).index(node) + 1}"
    path = tmp_path / "planned.json"
    path.write_text(json.dumps(skeleton))
    ClusterDeployment(ClusterSpec.from_file(path)).render(tmp_path / "planned")


def test_bundle_does_not_include_local_keys_or_results(tmp_path):
    cluster = tmp_path / "cluster"
    ClusterDeployment(["192.168.1.201", "192.168.1.202", "192.168.1.203"]).render(cluster)
    (cluster / "private.pem").write_text("do not publish")
    archive = tmp_path / "release.tar.gz"
    digest = MODULE["bundle"](archive, cluster)
    assert len(digest) == 64
    with tarfile.open(archive) as stream:
        names = stream.getnames()
    assert "cluster/node1/compose.json" in names
    assert "src/grpc_lb/controlplane/application.py" in names
    assert not any(
        name.startswith(("results/", ".venv/", ".git/")) or name.endswith(".pem") for name in names
    )


def test_ssh_stdin_preserves_linux_line_endings(monkeypatch, tmp_path):
    received = []

    def run(command, **kwargs):
        received.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout=b"checksum verified\n", stderr=b"")

    monkeypatch.setattr(MODULE["subprocess"], "run", run)
    ssh = MODULE["SshDeployment"](tmp_path / "key", tmp_path / "hosts")
    assert ssh.run("192.168.1.45", "sha256sum /tmp/release.tar.gz\r\n") == "checksum verified\n"
    assert received == [b"sha256sum /tmp/release.tar.gz\n"]


def test_deploy_reserves_every_service_port_before_starting_containers(monkeypatch, tmp_path):
    scripts = []
    monkeypatch.setattr(MODULE["subprocess"], "run", lambda *a, **k: SimpleNamespace(returncode=0))
    ssh = MODULE["SshDeployment"](tmp_path / "key", tmp_path / "hosts")
    monkeypatch.setattr(ssh, "run", lambda host, script, **k: scripts.append(script) or "abc  /tmp/x\n")
    ssh.upload("192.168.1.45", tmp_path / "release.tar.gz", "abc", "node1", builds=[])
    setup = next(script for script in scripts if "sysctl" in script)
    assert "net.ipv4.ip_local_reserved_ports=50101-50399" in setup
    assert setup.index("sysctl") < setup.index("docker")
    low, high = map(int, MODULE["RESERVED_PORTS"].split("-"))
    ports = [
        base + n for base in MODULE["PORT_BASE"].values() for n in (1, MODULE["MAX_INSTANCES_PER_SERVICE"])
    ]
    assert all(low <= port <= high for port in ports)


def test_spec_without_c_drops_it_from_the_call_graph(tmp_path):
    ips = ["10.0.0.11", "10.0.0.12", "10.0.0.13", "10.0.0.14"]
    raw = {
        "schema_version": 1,
        "nodes": {
            f"node{n}": {"ip": ip, "vcpus": 2, "instances": [f"a-{n}", f"b-{n}"]}
            for n, ip in enumerate(ips, 1)
        },
        "consul_servers": ["node1", "node2", "node3"],
        "controlplanes": ["node1", "node2"],
        "ingress": ["node1", "node2"],
    }
    path = tmp_path / "4a4b.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    spec = ClusterSpec.from_file(path)
    out = tmp_path / "cluster"
    ClusterDeployment(spec).render(out)
    topology = json.loads((out / "topology.json").read_text())
    assert set(topology["services"]) == {"service-a", "service-b"}
    assert topology["services"]["service-a"]["dependencies"] == ["service-b"]
    assert json.loads((out / "discovery.json").read_text())["services"] == {"service-b": []}
    load_topology(out / "topology.json")  # still a valid call graph
    for node in raw["nodes"].values():
        node["instances"] = node["instances"][:1]
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="service-b"):
        ClusterSpec.from_file(path)
