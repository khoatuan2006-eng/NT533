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
            assert services["ingress"]["ports"] == ["80:50050", "127.0.0.1:9901:9901"]
            assert "network_mode" not in services["ingress"]
        assert ("controlplane" in services) == (index < 3)
    envoy = (tmp_path / "node1/envoy.yaml").read_text()
    assert "address: 192.168.1.202, port_value: 50102" in envoy
    assert "port_value: 50050" in envoy


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
