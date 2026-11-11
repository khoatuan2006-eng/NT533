"""Three-VM driver: the commands it would send, checked without touching any VM."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import labenv  # noqa: E402
import labenv_ssh  # noqa: E402

DESCRIPTION = json.loads((ROOT / "configs/lab.cloud.example.json").read_text(encoding="utf-8"))


class FakeSsh(labenv_ssh.SshLab):
    def __init__(self):
        super().__init__(DESCRIPTION)
        self.sent = []
        self.replies = {}

    def ssh(self, node, script, *, check=True):
        self.sent.append((node, script))
        return SimpleNamespace(returncode=0, stdout=self.replies.get(node, ""), stderr="")


def test_lab_names_map_to_the_right_vm_and_compose_service():
    lab = FakeSsh()
    lab.stop("b-2", "consul-1")
    sent = dict(lab.sent)
    assert sent["node3"].endswith("stop -t 0 b-2") and sent["node1"].endswith("stop -t 0 consul")
    lab.sent.clear()
    lab.stop_node("node1")
    assert [node for node, _ in lab.sent] == ["node1"] and lab.sent[0][1].endswith("stop -t 0")
    with pytest.raises(ValueError):
        lab.stop("envoy-3")
    with pytest.raises(ValueError):
        lab.stop_node("node9")


def test_partition_rules_go_on_the_side_that_stays_reachable():
    lab = FakeSsh()
    # Isolating node3: rules must not be installed on node3, which sits behind the jump node.
    lab.partition(["node3"], ["node1", "node2"])
    assert [node for node, _ in lab.sent] == ["node1", "node2"]
    for _, script in lab.sent:
        assert "-s 192.168.1.217 -j DROP" in script and "-d 192.168.1.217 -j DROP" in script
        assert "192.168.1.238" not in script and "192.168.1.172" not in script
        assert "-I FORWARD -j LABFAULT" in script and f"sleep {labenv_ssh.FAULT_TTL_S}" in script
    lab.sent.clear()
    # Isolating the jump node itself: only it can be reached, and it blocks both peers.
    lab.partition(["node1"], ["node2", "node3"])
    assert [node for node, _ in lab.sent] == ["node1"]
    assert "192.168.1.172" in lab.sent[0][1] and "192.168.1.217" in lab.sent[0][1]
    with pytest.raises(ValueError):
        lab.partition(["node1"], ["node1"])


def test_degrade_only_touches_traffic_to_lab_peers_and_expires():
    lab = FakeSsh()
    lab.degrade("node3", delay_ms=100)
    node, script = lab.sent[0]
    assert node == "node3" and "netem delay 100ms" in script
    assert "match ip dst 192.168.1.238/32" in script and "match ip dst 192.168.1.172/32" in script
    assert "priomap 0 0 0 0" in script and "sleep" in script and "$dev" in script
    with pytest.raises(ValueError):
        lab.degrade("node3")


def test_heal_starts_with_the_jump_node():
    lab = FakeSsh()
    lab.heal()
    assert [node for node, _ in lab.sent] == ["node1", "node2", "node3"]
    assert all("iptables -F LABFAULT" in script and "tc qdisc del" in script for _, script in lab.sent)


def test_variant_writes_mode_override_and_ingress_policy():
    lab = FakeSsh()
    lab.up_variant("static-lr-ingress")
    sent = dict(lab.sent)
    assert (
        '"DISCOVERY_MODE": "static"' in sent["node1"]
        and '"STATIC_LB_POLICY": "least_request"' in sent["node1"]
    )
    assert "lb_policy: LEAST_REQUEST" in sent["node2"] and "ingress" in sent["node2"]
    assert "envoy.yaml" not in sent["node3"]
    with pytest.raises(ValueError):
        lab.up("xds", build=True)
    with pytest.raises(RuntimeError):
        lab.down()


def test_containers_and_leader_are_read_from_compose_output():
    lab = FakeSsh()
    lab.replies = {
        "node1": '[{"Service":"consul","State":"running"},{"Service":"a-1","State":"exited"}]',
        "node2": '{"Service":"consul","State":"running"}\n{"Service":"c-1","State":"running"}',
        "node3": "",
    }
    rows = {row["service"]: row for row in lab.containers()}
    assert rows["consul-1"]["running"] and not rows["a-1"]["running"] and rows["c-1"]["running"]
    assert not rows["b-2"]["running"] and rows["b-2"]["ips"] == ["192.168.1.217"]
    lab.replies = {"node1": "Node ID Address State Voter\nlb-node2 x 192.168.1.172:8300 leader true\n"}
    assert lab.consul_leader() == "consul-2"


def test_make_lab_selects_driver(tmp_path):
    assert isinstance(labenv.make_lab(), labenv.ComposeLab)
    assert isinstance(labenv.make_lab(env=ROOT / "configs/lab.cloud.example.json"), labenv_ssh.SshLab)
    bad = tmp_path / "bad.json"
    bad.write_text('{"driver": "k8s"}', encoding="utf-8")
    with pytest.raises(ValueError):
        labenv.make_lab(env=bad)
