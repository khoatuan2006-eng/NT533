"""Lab driver and fault report logic that can be checked without Docker."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import labenv  # noqa: E402
import run_faults  # noqa: E402


class FakeLab(labenv.ComposeLab):
    """ComposeLab with a recorded shell instead of a Docker host."""

    def __init__(self):
        super().__init__()
        self.scripts = []
        self.rows = [
            {
                "name": "lab-a-1",
                "service": "a-1",
                "node": "node1",
                "running": True,
                "pid": 11,
                "ips": ["10.0.0.1"],
            },
            {
                "name": "lab-c-1",
                "service": "c-1",
                "node": "node2",
                "running": True,
                "pid": 22,
                "ips": ["10.0.0.2"],
            },
            {
                "name": "lab-b-2",
                "service": "b-2",
                "node": "node3",
                "running": True,
                "pid": 33,
                "ips": ["10.0.0.3"],
            },
            {"name": "lab-c-2", "service": "c-2", "node": "node3", "running": False, "pid": 0, "ips": []},
        ]

    def containers(self):
        return self.rows

    def sh(self, script, *, root=False, check=True):
        self.scripts.append((script, root))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_windows_paths_map_to_wsl_mounts():
    assert (
        labenv.posix_path(r"D:\LoadBalance\deploy\compose.lab.yaml")
        == "/mnt/d/LoadBalance/deploy/compose.lab.yaml"
    )
    assert labenv.posix_path("/srv/lab/compose.yaml") == "/srv/lab/compose.yaml"


def test_partition_blocks_both_directions_only_inside_one_side():
    lab = FakeLab()
    lab.partition(["node3"], ["node1", "node2"])
    script, root = lab.scripts[-1]
    assert root
    # Only the running node3 container gets rules, one pair per peer address.
    assert script.count("nsenter -t 33 -n") == 1 and "nsenter -t 11" not in script and "-t 0 " not in script
    for ip in ("10.0.0.1", "10.0.0.2"):
        assert f"iptables -I INPUT -s {ip} -j DROP" in script
        assert f"iptables -I OUTPUT -d {ip} -j DROP" in script
    assert "10.0.0.3" not in script


@pytest.mark.parametrize("sides", [([], ["node1"]), (["node1"], ["node1"]), (["node1"], ["node9"])])
def test_partition_rejects_empty_overlapping_or_unknown_groups(sides):
    with pytest.raises(ValueError):
        FakeLab().partition(*sides)


def test_degrade_and_heal_target_running_containers():
    lab = FakeLab()
    lab.degrade("node3", delay_ms=100, loss_percent=5)
    script, root = lab.scripts[-1]
    assert root and "netem delay 100ms loss 5.0%" in script and script.count("nsenter") == 1
    with pytest.raises(ValueError):
        lab.degrade("node3")
    lab.heal()
    script, _ = lab.scripts[-1]
    assert script.count("iptables -F INPUT") == 3 and "tc qdisc del dev eth0 root" in script


def test_stop_node_stops_every_container_of_that_node():
    lab = FakeLab()
    lab.stop_node("node3")
    assert lab.scripts[-1][0] == "docker stop -t 0 lab-b-2 lab-c-2"
    with pytest.raises(ValueError):
        lab.stop("missing")


def write_trace(directory, name, expect, failing, *, code="UNAVAILABLE", mode="xds"):
    """60 s at 10 RPS; fault from 10 s to 40 s; `failing` is the [start, end) error interval."""
    rows = []
    for index in range(600):
        at = index / 10
        ok = not failing[0] <= at < failing[1]
        row = {
            "index": index,
            "scheduled_s": at,
            "code": "OK" if ok else code,
            "latency_ms": 12.0,
            "schedule_lag_ms": 0,
        }
        if ok:
            row["instances"] = ["a-1", "b-1", "c-1"]
        rows.append(row)
    (directory / f"{name}.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n", encoding="utf-8")
    events = {"fault": name.rsplit("-", 2)[0], "mode": mode, "expect": expect}
    events |= {"fault_dispatch_s": 10.0, "restore_dispatch_s": 40.0}
    (directory / f"{name}.events.json").write_text(json.dumps(events), encoding="utf-8")


def test_report_measures_interruption_and_applies_each_expectation(tmp_path):
    write_trace(tmp_path, "backend-stop-xds-r1", "continue", (12, 14))
    write_trace(tmp_path, "backend-stop-xds-r2", "continue", (0, 0))
    write_trace(tmp_path, "quorum-loss-xds-r1", "fail-closed", (25, 43))
    write_trace(tmp_path, "node1-down-xds-r1", "continue", (12, 40))
    text = run_faults.report(tmp_path)
    summary = {(row["fault"], row["mode"]): row for row in json.loads((tmp_path / "report.json").read_text())}

    stop = summary[("backend-stop", "xds")]
    assert stop["verdict"] == "đạt"
    assert sorted(run["interruption_s"] for run in stop["runs"]) == [0, pytest.approx(2.0)]
    assert stop["runs"][0]["instances_serving_in_fault"] == ["a-1", "b-1", "c-1"]

    quorum = summary[("quorum-loss", "xds")]
    assert quorum["verdict"] == "đạt"
    assert quorum["runs"][0]["error_codes_in_fault"] == {"UNAVAILABLE": 150}
    assert quorum["runs"][0]["recovery_after_restore_s"] == pytest.approx(3.0)

    # Errors that last until the fault is lifted never converged while it was active.
    assert summary[("node1-down", "xds")]["verdict"].startswith("KHÔNG ĐẠT")
    assert "Dừng một backend (b-2)" in text and (tmp_path / "report.md").exists()


def test_fail_closed_fails_when_callers_keep_serving(tmp_path):
    write_trace(tmp_path, "quorum-loss-xds-r1", "fail-closed", (0, 0))
    run_faults.report(tmp_path)
    assert json.loads((tmp_path / "report.json").read_text())[0]["verdict"].startswith("KHÔNG ĐẠT")
