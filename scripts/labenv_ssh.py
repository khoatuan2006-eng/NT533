"""Three-VM lab driver: the same fault and load suites, driven over SSH.

Each node is one VM running its part of the stack with Docker Compose, as deployed by
scripts/multihost.py. This driver implements the LabEnvironment interface from labenv.py:

  process faults  docker compose stop/start on the node that owns the service
  node down       stop every lab container on that VM (the VM itself stays up)
  partition       iptables DROP rules between the nodes' private addresses
  degrade         tc netem on traffic towards the other lab nodes only
  heal            flush the rules; every rule also expires on its own (see FAULT_TTL_S)

The environment is described by a JSON file, see configs/lab.cloud.json.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Network faults remove themselves after this long, so a lost SSH session cannot leave
# the VMs cut off from each other.
FAULT_TTL_S = 900
CHAIN = "LABFAULT"
# One person at a time: the whole team shares these three VMs.
LOCK_PATH = "/var/tmp/loadbalance-lab.lock"


class LabBusy(RuntimeError):
    """Another team member holds the cluster."""


class SshLab:
    """Lab on three VMs. Service names are the lab's (consul-1, envoy-2, b-1, ...)."""

    def __init__(self, description: dict, *, base: Path = ROOT):
        try:
            self.address = description["address"]
            self.ssh_config = str((base / description["ssh_config"]).resolve())
            self.project = description.get("project", "lb-cloud")
            self.remote_dir = description.get("remote_dir", "/opt/loadbalance/cluster")
            self.jump_node = description.get("jump_node", "")
            self.nodes = {name: dict(node) for name, node in description["nodes"].items()}
            for name, node in self.nodes.items():
                if not node["ssh"] or not node["ip"] or not node["services"]:
                    raise ValueError(f"node {name} needs ssh, ip and services")
        except (KeyError, TypeError) as error:
            raise ValueError(f"invalid lab description: {error}") from error
        # lab service name -> (node, compose service name on that VM)
        self.services: dict[str, tuple[str, str]] = {}
        for name, node in self.nodes.items():
            for lab_name, remote_name in node["services"].items():
                if lab_name in self.services:
                    raise ValueError(f"service listed on two nodes: {lab_name}")
                self.services[lab_name] = (name, remote_name)
        if self.jump_node and self.jump_node not in self.nodes:
            raise ValueError("jump_node must be one of the nodes")

    # ---- command execution ---------------------------------------------------
    def ssh(self, node: str, script: str, *, check: bool = True) -> subprocess.CompletedProcess:
        """Run a shell script on one VM. The script travels on stdin."""
        command = ["ssh", "-F", self.ssh_config, self.nodes[node]["ssh"], "sh -s"]
        for attempt in range(3):
            result = subprocess.run(
                command, input=script.encode(), capture_output=True, timeout=180, check=False
            )
            # 255 is ssh's own failure (connection), before the script ran; scripts here are idempotent.
            if result.returncode != 255 or attempt == 2:
                break
            time.sleep(2)
        result.stdout = result.stdout.decode("utf-8", "replace")
        result.stderr = result.stderr.decode("utf-8", "replace")
        if check and result.returncode:
            raise RuntimeError(
                f"{node}: command failed ({result.returncode}): {script.strip()[:200]}\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def _compose(self, node: str) -> str:
        directory = f"{self.remote_dir}/{node}"
        files = f"-f {directory}/compose.json"
        return (
            f"sudo docker compose -p {shlex.quote(self.project)} {files} "
            f"$(test -f {directory}/mode.json && echo -f {directory}/mode.json)"
        )

    def _by_node(self, services) -> dict[str, list[str]]:
        missing = [name for name in services if name not in self.services]
        if missing:
            raise ValueError(f"unknown lab service: {', '.join(sorted(missing))}")
        grouped: dict[str, list[str]] = {}
        for name in services:
            node, remote = self.services[name]
            grouped.setdefault(node, []).append(remote)
        return grouped

    def _ordered(self, nodes) -> list[str]:
        """Jump node first: once it is healed, the nodes behind it are reachable again."""
        return sorted(nodes, key=lambda name: (name != self.jump_node, name))

    @contextlib.contextmanager
    def keepalive(self):
        yield

    # ---- shared-cluster lock -------------------------------------------------
    def _lock_node(self) -> str:
        return self.jump_node or next(iter(self.nodes))

    def lock_holder(self) -> str | None:
        """Who holds the cluster right now, or None."""
        result = self.ssh(self._lock_node(), f"cat {LOCK_PATH}/owner 2>/dev/null || true")
        return result.stdout.strip() or None

    @contextlib.contextmanager
    def lock(self, purpose: str):
        """Hold the cluster for one run; refuse if someone else holds it."""
        owner = f"{getpass.getuser()}@{socket.gethostname()} từ {time.strftime('%Y-%m-%d %H:%M')}: {purpose}"
        script = (
            f"if mkdir {LOCK_PATH} 2>/dev/null; then printf %s {shlex.quote(owner)} > {LOCK_PATH}/owner; "
            f"echo ACQUIRED; else cat {LOCK_PATH}/owner 2>/dev/null; fi"
        )
        result = self.ssh(self._lock_node(), script)
        if "ACQUIRED" not in result.stdout:
            holder = result.stdout.strip() or "không rõ"
            raise LabBusy(
                f"Cụm đang được dùng bởi {holder}. Chờ người đó chạy xong; nếu chắc chắn khóa bị bỏ lại "
                "(máy người đó đã tắt), xem `labenv.py --env configs/lab.cloud.json lock-status` rồi `unlock`."
            )
        try:
            yield
        finally:
            # Release only our own lock, never one someone else took after a forced unlock.
            self.ssh(
                self._lock_node(),
                f'[ "$(cat {LOCK_PATH}/owner 2>/dev/null)" = {shlex.quote(owner)} ] && rm -rf {LOCK_PATH}; true',
                check=False,
            )

    def unlock(self) -> None:
        self.ssh(self._lock_node(), f"rm -rf {LOCK_PATH}")

    def ensure_free(self) -> None:
        holder = self.lock_holder()
        if holder:
            raise LabBusy(f"Cụm đang được dùng bởi {holder}; không thao tác tay lúc này.")

    # ---- lifecycle -----------------------------------------------------------
    def up(
        self,
        mode: str = "xds",
        *,
        build: bool = False,
        static_policy: str = "round_robin",
        ingress_policy: str = "round_robin",
    ) -> None:
        if build:
            raise ValueError("deploy new code to the VMs with scripts/multihost.py, not --build")
        if mode not in ("static", "xds") or {static_policy, ingress_policy} - {
            "round_robin",
            "least_request",
        }:
            raise ValueError("invalid mode or policy")
        for node, info in self.nodes.items():
            directory = f"{self.remote_dir}/{node}"
            callers = [remote for lab, remote in info["services"].items() if lab.startswith("a-")]
            environment = {"DISCOVERY_MODE": mode, "STATIC_LB_POLICY": static_policy}
            override = json.dumps({"services": {name: {"environment": environment} for name in callers}})
            script = [
                "set -e",
                f"printf %s {shlex.quote(override)} | sudo tee {directory}/mode.json >/dev/null",
                f"{self._compose(node)} up -d --no-build",
            ]
            if any(lab.startswith("envoy-") for lab in info["services"]):
                # Envoy reads its file only at start: rewrite the policy, recreate only if it changed.
                script += [
                    f"before=$(sudo md5sum {directory}/envoy.yaml)",
                    f"sudo sed -i -E 's/lb_policy: (ROUND_ROBIN|LEAST_REQUEST)/lb_policy: "
                    f"{ingress_policy.upper()}/' {directory}/envoy.yaml",
                    f'[ "$before" = "$(sudo md5sum {directory}/envoy.yaml)" ] || '
                    f"{self._compose(node)} up -d --no-build --no-deps --force-recreate ingress",
                ]
            self.ssh(node, "\n".join(script))

    def up_variant(self, variant: str, *, build: bool = False) -> None:
        from labenv import VARIANTS

        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of: {', '.join(VARIANTS)}")
        mode, static_policy, ingress_policy = VARIANTS[variant]
        self.up(mode, build=build, static_policy=static_policy, ingress_policy=ingress_policy)

    def down(self) -> None:
        raise RuntimeError("the cloud lab is shared; stop it deliberately on the VMs, not from this tool")

    def containers(self) -> list[dict]:
        rows = []
        for node, info in self.nodes.items():
            output = self.ssh(node, f"{self._compose(node)} ps -a --format json").stdout.strip()
            # Compose prints either one JSON array or one JSON object per line, depending on version.
            entries = (
                json.loads(output)
                if output.startswith("[")
                else [json.loads(x) for x in output.splitlines() if x]
            )
            state = {entry["Service"]: entry.get("State") == "running" for entry in entries}
            for lab_name, remote in info["services"].items():
                rows.append(
                    {
                        "name": f"{node}/{remote}",
                        "service": lab_name,
                        "node": node,
                        "running": state.get(remote, False),
                        "pid": 0,
                        "ips": [info["ip"]],
                    }
                )
        return sorted(rows, key=lambda row: (row["node"], row["service"]))

    # ---- process faults ------------------------------------------------------
    def stop(self, *services: str) -> None:
        for node, names in self._by_node(services).items():
            self.ssh(node, f"{self._compose(node)} stop -t 0 {shlex.join(names)}")

    def start(self, *services: str) -> None:
        for node, names in self._by_node(services).items():
            self.ssh(node, f"{self._compose(node)} start {shlex.join(names)}")

    def stop_node(self, node: str) -> None:
        self._known(node)
        self.ssh(node, f"{self._compose(node)} stop -t 0")

    def start_node(self, node: str) -> None:
        self._known(node)
        self.ssh(node, f"{self._compose(node)} start")

    def _known(self, *nodes: str) -> None:
        unknown = set(nodes) - set(self.nodes)
        if unknown:
            raise ValueError(f"unknown lab node: {', '.join(sorted(unknown))}")

    # ---- network faults ------------------------------------------------------
    def _expire(self, command: str) -> str:
        # Double quotes: $dev is expanded now, by the shell that knows it.
        return f'(nohup sh -c "sleep {FAULT_TTL_S}; {command}" >/dev/null 2>&1 &)'

    def partition(self, side_a: list[str], side_b: list[str]) -> None:
        """Drop all traffic between two groups of nodes, in both directions."""
        if not side_a or not side_b or set(side_a) & set(side_b):
            raise ValueError("partition needs two disjoint, nonempty groups of nodes")
        self._known(*side_a, *side_b)
        # Rules on one side are enough. Put them on the side we can still reach afterwards:
        # the one holding the SSH jump node, because the other side sits behind it.
        if self.jump_node in side_b:
            side_a, side_b = side_b, side_a
        blocked = [self.nodes[name]["ip"] for name in side_b]
        script = [
            "set -e",
            f"sudo iptables -N {CHAIN} 2>/dev/null || true",
            # FORWARD too: the bridged Envoy container leaves the VM through it.
            *(
                f"sudo iptables -C {hook} -j {CHAIN} 2>/dev/null || sudo iptables -I {hook} -j {CHAIN}"
                for hook in ("INPUT", "OUTPUT", "FORWARD")
            ),
            f"sudo iptables -F {CHAIN}",
            *(
                f"sudo iptables -A {CHAIN} {direction} {ip} -j DROP"
                for ip in blocked
                for direction in ("-s", "-d")
            ),
            self._expire(f"sudo iptables -F {CHAIN}"),
        ]
        for node in self._ordered(side_a):
            self.ssh(node, "\n".join(script))

    def degrade(self, node: str, *, delay_ms: int = 0, loss_percent: float = 0) -> None:
        """Delay and/or drop packets this node sends to the other lab nodes; SSH is not affected."""
        if delay_ms < 0 or not 0 <= loss_percent < 100 or (not delay_ms and not loss_percent):
            raise ValueError("delay_ms >= 0, 0 <= loss_percent < 100, and at least one must be set")
        self._known(node)
        peers = [info["ip"] for name, info in self.nodes.items() if name != node]
        netem = (f" delay {int(delay_ms)}ms" if delay_ms else "") + (
            f" loss {float(loss_percent)}%" if loss_percent else ""
        )
        script = [
            "set -e",
            f"dev=$(ip -o route get {peers[0]} | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p')",
            # All traffic stays in band 1; only packets to lab peers are steered into the netem band.
            'sudo tc qdisc replace dev "$dev" root handle 1: prio bands 3 priomap 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0',
            f'sudo tc qdisc replace dev "$dev" parent 1:3 handle 30: netem{netem}',
            *(
                f'sudo tc filter add dev "$dev" protocol ip parent 1:0 prio 3 u32 match ip dst {ip}/32 flowid 1:3'
                for ip in peers
            ),
            self._expire("sudo tc qdisc del dev $dev root"),
        ]
        self.ssh(node, "\n".join(script))

    def heal(self) -> None:
        """Remove every partition and degradation; safe to call when none is active."""
        peers = {
            name: next(info["ip"] for other, info in self.nodes.items() if other != name)
            for name in self.nodes
        }
        errors = []
        for node in self._ordered(self.nodes):
            script = (
                f"sudo iptables -F {CHAIN} 2>/dev/null\n"
                f"dev=$(ip -o route get {peers[node]} | sed -n 's/.* dev \\([^ ]*\\).*/\\1/p')\n"
                'sudo tc qdisc del dev "$dev" root 2>/dev/null\ntrue'
            )
            try:
                self.ssh(node, script)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
        if errors:
            raise RuntimeError("heal incomplete; rules expire on their own:\n" + "\n".join(errors))

    # ---- registry ------------------------------------------------------------
    def consul_leader(self) -> str:
        """Lab name of the Consul server that currently leads the Raft quorum."""
        by_ip = {info["ip"]: name for name, info in self.nodes.items()}
        for node, info in self.nodes.items():
            remote = next((r for lab, r in info["services"].items() if lab.startswith("consul-")), None)
            if remote is None:
                continue
            command = f"{self._compose(node)} exec -T {remote} consul operator raft list-peers"
            for line in self.ssh(node, command, check=False).stdout.splitlines():
                fields = line.split()
                if "leader" in fields:
                    leader_ip = next(
                        (field.rsplit(":", 1)[0] for field in fields if field.rsplit(":", 1)[0] in by_ip), ""
                    )
                    if leader_ip:
                        owner = by_ip[leader_ip]
                        return next(lab for lab in self.nodes[owner]["services"] if lab.startswith("consul-"))
        raise RuntimeError("no Consul leader visible")

    def provenance(self) -> dict:
        def local(*command: str) -> str:
            try:
                return subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, timeout=30
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return ""

        hosts = {}
        for node in self.nodes:
            output = self.ssh(
                node,
                "echo kernel=$(uname -r); echo cpus=$(nproc); "
                "echo mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo); "
                "echo docker=$(sudo docker version --format '{{.Server.Version}}'); "
                "echo release=$(sha256sum /opt/loadbalance/release.tar.gz 2>/dev/null | cut -c1-16)",
                check=False,
            ).stdout
            hosts[node] = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        return {
            "driver": "ssh",
            "git_commit": local("git", "rev-parse", "HEAD"),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "address": self.address,
            "nodes": hosts,
        }

    def check(self) -> dict[str, bool]:
        """Read-only: can we reach every VM and does it have what the lab needs?"""
        result = {}
        self.check_errors: dict[str, str] = {}
        for node in self._ordered(self.nodes):
            probe = self.ssh(
                node,
                "sudo -n true && sudo docker compose version >/dev/null && "
                "command -v iptables >/dev/null && command -v tc >/dev/null && "
                f"test -f {self.remote_dir}/{node}/compose.json",
                check=False,
            )
            result[node] = probe.returncode == 0
            if probe.returncode:
                self.check_errors[node] = (probe.stderr or probe.stdout).strip() or f"exit {probe.returncode}"
        return result
