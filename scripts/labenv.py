"""Lab environments: one interface for node, network and registry faults.

Experiments only call LabEnvironment methods, so the same fault suite runs on any
environment that implements them. ComposeLab is the single-host driver: three logical
nodes are groups of containers sharing a lab.node label (deploy/compose.lab.yaml).

    python scripts/labenv.py check
    python scripts/labenv.py up --variant xds --build
    python scripts/labenv.py status
    python scripts/labenv.py partition --a node3 --b node1 node2
    python scripts/labenv.py heal
    python scripts/labenv.py hold      # Windows: keep WSL (and the lab) alive for manual work
    python scripts/labenv.py down
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
NODES = ("node1", "node2", "node3")
# name -> (caller discovery mode, caller policy for the static pool, Envoy ingress policy).
# gRPC Python 1.84 has no least_request policy, so the xDS caller can only be round_robin.
VARIANTS = {
    "xds": ("xds", "round_robin", "round_robin"),
    "static": ("static", "round_robin", "round_robin"),
    "static-lr": ("static", "least_request", "round_robin"),
    "xds-lr-ingress": ("xds", "round_robin", "least_request"),
    "static-lr-ingress": ("static", "least_request", "least_request"),
}
ENVOY_SOURCE = ROOT / "deploy/envoy.microservices.yaml"
ENVOY_GENERATED = ROOT / "deploy/.lab/envoy.yaml"


class LabEnvironment(Protocol):
    """What a fault experiment may do to an environment."""

    address: str  # gRPC target of the ingress, reachable from the load generator

    def up(self, mode: str = "xds", *, build: bool = False) -> None: ...
    def up_variant(self, variant: str, *, build: bool = False) -> None: ...
    def down(self) -> None: ...
    def containers(self) -> list[dict]: ...
    def stop(self, *services: str) -> None: ...
    def start(self, *services: str) -> None: ...
    def stop_node(self, node: str) -> None: ...
    def start_node(self, node: str) -> None: ...
    def partition(self, side_a: list[str], side_b: list[str]) -> None: ...
    def degrade(self, node: str, *, delay_ms: int = 0, loss_percent: float = 0) -> None: ...
    def heal(self) -> None: ...
    def consul_leader(self) -> str: ...
    def provenance(self) -> dict: ...


def posix_path(path: Path | str) -> str:
    """Translate a Windows path to its WSL mount; leave POSIX paths unchanged."""
    text = str(path)
    drive = PureWindowsPath(text).drive
    if len(drive) == 2 and drive[1] == ":":
        return f"/mnt/{drive[0].lower()}{PureWindowsPath(text).as_posix()[2:]}"
    return text.replace("\\", "/")


class ComposeLab:
    """Three logical nodes on one Docker host; on Windows Docker runs inside WSL."""

    address = "ipv4:127.0.0.1:50050,127.0.0.1:50060"

    def __init__(self, *, wsl: str | None = None, compose: str | None = None, project: str = "lb-lab"):
        self.windows = sys.platform == "win32"
        self.wsl = wsl if wsl is not None else os.environ.get("LAB_WSL", "")
        self.compose_binary = compose or os.environ.get("LAB_COMPOSE", "")
        self.project = project
        self.file = (
            posix_path(ROOT / "deploy/compose.lab.yaml")
            if self.windows
            else str(ROOT / "deploy/compose.lab.yaml")
        )
        self._compose: list[str] | None = None

    # ---- command execution ---------------------------------------------------
    def sh(self, script: str, *, root: bool = False, check: bool = True) -> subprocess.CompletedProcess:
        """Run a shell script on the Docker host. The script travels on stdin, so no
        argument is re-parsed by wsl.exe or an intermediate shell."""
        if self.windows:
            prefix = ["wsl", *(["-d", self.wsl] if self.wsl else []), "-u", "root", "--"]
        elif root and os.geteuid() != 0:
            prefix = ["sudo", "-n"]
        else:
            prefix = []
        result = subprocess.run(
            [*prefix, "sh", "-s"], input=script.encode(), capture_output=True, timeout=600, check=False
        )
        result.stdout = result.stdout.decode("utf-8", "replace").replace("\x00", "")
        result.stderr = result.stderr.decode("utf-8", "replace").replace("\x00", "")
        if "Wsl/Service/" in result.stdout + result.stderr:
            # The Docker host itself is unreachable; no lab command can succeed.
            raise RuntimeError(
                "WSL is not answering ("
                + " ".join((result.stdout + result.stderr).split())
                + "). Run `wsl --shutdown`, then retry; restart Windows if it keeps failing."
            )
        if check and result.returncode:
            raise RuntimeError(
                f"lab command failed ({result.returncode}): {script.strip()[:200]}\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def compose(self, *args: str, env: dict[str, str] | None = None) -> str:
        if self._compose is None:
            local = ROOT / ".venv/acceptance-tools/docker-compose-linux-x86_64"
            candidates = [[self.compose_binary]] if self.compose_binary else []
            candidates += [["docker", "compose"], ["docker-compose"]]
            if local.exists():
                candidates.append([posix_path(local) if self.windows else str(local)])
            for candidate in candidates:
                if not self.sh(shlex.join([*candidate, "version"]), check=False).returncode:
                    self._compose = candidate
                    break
            else:
                raise RuntimeError("Docker Compose not found; install the plugin or set LAB_COMPOSE")
        assignments = [f"{key}={value}" for key, value in (env or {}).items()]
        command = ["env", *assignments, *self._compose, "-p", self.project, "-f", self.file, *args]
        return self.sh(shlex.join(command)).stdout

    # ---- lifecycle -----------------------------------------------------------
    @contextlib.contextmanager
    def keepalive(self):
        """Hold one WSL session open while the lab is in use.

        WSL stops an idle distribution a few seconds after its last session ends, which
        restarts every container and drops the localhost port forwarding mid-experiment.
        """
        if not self.windows:
            yield
            return
        holder = subprocess.Popen(
            ["wsl", *(["-d", self.wsl] if self.wsl else []), "-u", "root", "--", "sleep", "infinity"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            yield
        finally:
            holder.terminate()
            holder.wait(timeout=10)

    def up(
        self,
        mode: str = "xds",
        *,
        build: bool = False,
        static_policy: str = "round_robin",
        ingress_policy: str = "round_robin",
    ) -> None:
        if mode not in ("static", "xds"):
            raise ValueError("mode must be static or xds")
        if static_policy not in ("round_robin", "least_request") or ingress_policy not in (
            "round_robin",
            "least_request",
        ):
            raise ValueError("policies must be round_robin or least_request")
        source = ENVOY_SOURCE.read_text(encoding="utf-8")
        assert "lb_policy: ROUND_ROBIN" in source
        config = source.replace("lb_policy: ROUND_ROBIN", f"lb_policy: {ingress_policy.upper()}")
        previous = ENVOY_GENERATED.read_text(encoding="utf-8") if ENVOY_GENERATED.exists() else None
        ENVOY_GENERATED.parent.mkdir(exist_ok=True)
        ENVOY_GENERATED.write_bytes(config.encode("utf-8"))
        env = {"LAB_DISCOVERY_MODE": mode, "LAB_STATIC_POLICY": static_policy}
        if build:
            # Services share two images; building every service in parallel races on the shared tag.
            self.compose("build", "a-1", "controlplane-1", env=env)
        self.compose("up", "-d", env=env)
        if previous is not None and previous != config:
            # A bind-mounted file changes without recreating the container; Envoy only reads it at start.
            self.compose("up", "-d", "--no-deps", "--force-recreate", "envoy-1", "envoy-2", env=env)

    def up_variant(self, variant: str, *, build: bool = False) -> None:
        if variant not in VARIANTS:
            raise ValueError(f"variant must be one of: {', '.join(VARIANTS)}")
        mode, static_policy, ingress_policy = VARIANTS[variant]
        self.up(mode, build=build, static_policy=static_policy, ingress_policy=ingress_policy)

    def down(self) -> None:
        self.compose("down", "-v", "--remove-orphans")

    def containers(self) -> list[dict]:
        # One host round trip: each call through wsl.exe costs over a second.
        listing = f"docker ps -a -q --filter label=com.docker.compose.project={shlex.quote(self.project)}"
        output = self.sh(f'ids=$({listing}); [ -z "$ids" ] || docker inspect $ids').stdout.strip()
        if not output:
            return []
        rows = json.loads(output)
        return sorted(
            (
                {
                    "name": row["Name"].lstrip("/"),
                    "service": row["Config"]["Labels"].get("com.docker.compose.service", ""),
                    "node": row["Config"]["Labels"].get("lab.node", ""),
                    "running": bool(row["State"]["Running"]),
                    "pid": row["State"]["Pid"],
                    "ips": sorted(
                        net["IPAddress"]
                        for net in row["NetworkSettings"]["Networks"].values()
                        if net.get("IPAddress")
                    ),
                }
                for row in rows
            ),
            key=lambda row: (row["node"], row["service"]),
        )

    def _select(self, *, services=(), node=None, running=None) -> list[dict]:
        rows = self.containers()
        missing = set(services) - {row["service"] for row in rows}
        if missing:
            raise ValueError(f"unknown lab service: {', '.join(sorted(missing))}")
        if node is not None and node not in {row["node"] for row in rows}:
            raise ValueError(f"unknown lab node: {node}")
        return [
            row
            for row in rows
            if (not services or row["service"] in services)
            and (node is None or row["node"] == node)
            and (running is None or row["running"] == running)
        ]

    # ---- process faults ------------------------------------------------------
    def stop(self, *services: str) -> None:
        names = [row["name"] for row in self._select(services=services)]
        self.sh(shlex.join(["docker", "stop", "-t", "0", *names]))

    def start(self, *services: str) -> None:
        names = [row["name"] for row in self._select(services=services)]
        self.sh(shlex.join(["docker", "start", *names]))

    def stop_node(self, node: str) -> None:
        self.sh(shlex.join(["docker", "stop", "-t", "0", *[row["name"] for row in self._select(node=node)]]))

    def start_node(self, node: str) -> None:
        self.sh(shlex.join(["docker", "start", *[row["name"] for row in self._select(node=node)]]))

    # ---- network faults ------------------------------------------------------
    def _in_namespace(self, row: dict, commands: list[str]) -> str:
        return shlex.join(["nsenter", "-t", str(row["pid"]), "-n", "sh", "-c", "; ".join(commands)])

    def partition(self, side_a: list[str], side_b: list[str]) -> None:
        """Drop all traffic between two groups of nodes, in both directions."""
        if not side_a or not side_b or set(side_a) & set(side_b):
            raise ValueError("partition needs two disjoint, nonempty groups of nodes")
        rows = [row for row in self.containers() if row["running"]]
        unknown = (set(side_a) | set(side_b)) - {row["node"] for row in rows}
        if unknown:
            raise ValueError(f"unknown or stopped lab node: {', '.join(sorted(unknown))}")
        blocked = [ip for row in rows if row["node"] in side_b for ip in row["ips"]]
        script = [
            self._in_namespace(
                row,
                [
                    rule
                    for ip in blocked
                    for rule in (f"iptables -I INPUT -s {ip} -j DROP", f"iptables -I OUTPUT -d {ip} -j DROP")
                ],
            )
            for row in rows
            if row["node"] in side_a
        ]
        self.sh("set -e\n" + "\n".join(script), root=True)

    def degrade(self, node: str, *, delay_ms: int = 0, loss_percent: float = 0) -> None:
        """Add egress delay and/or packet loss to every container of one node."""
        if delay_ms < 0 or not 0 <= loss_percent < 100 or (not delay_ms and not loss_percent):
            raise ValueError("delay_ms >= 0, 0 <= loss_percent < 100, and at least one must be set")
        netem = "tc qdisc replace dev eth0 root netem"
        netem += f" delay {int(delay_ms)}ms" if delay_ms else ""
        netem += f" loss {float(loss_percent)}%" if loss_percent else ""
        rows = self._select(node=node, running=True)
        self.sh("set -e\n" + "\n".join(self._in_namespace(row, [netem]) for row in rows), root=True)

    def heal(self) -> None:
        """Remove every partition and degradation; safe to call when none is active."""
        cleanup = [
            "iptables -F INPUT",
            "iptables -F OUTPUT",
            "tc qdisc del dev eth0 root 2>/dev/null",
            "true",
        ]
        rows = [row for row in self.containers() if row["running"]]
        if rows:
            self.sh("\n".join(self._in_namespace(row, cleanup) for row in rows), root=True)

    # ---- registry ------------------------------------------------------------
    def consul_leader(self) -> str:
        """Service name of the Consul server that currently leads the Raft quorum."""
        for row in self._select(running=True):
            if not row["service"].startswith("consul-"):
                continue
            result = self.sh(
                shlex.join(["docker", "exec", row["name"], "consul", "operator", "raft", "list-peers"]),
                check=False,
            )
            for line in result.stdout.splitlines():
                fields = line.split()
                if "leader" in fields:
                    return fields[0]
        raise RuntimeError("no Consul leader visible")

    def provenance(self) -> dict:
        """Which code and host produced a result, so a number can be traced back to its setup."""

        def local(*command: str) -> str:
            try:
                return subprocess.run(
                    command, cwd=ROOT, capture_output=True, text=True, timeout=30
                ).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return ""

        host = self.sh(
            "echo kernel=$(uname -r); echo cpus=$(nproc); "
            "echo mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo); "
            "echo docker=$(docker version --format '{{.Server.Version}}')",
            check=False,
        ).stdout
        return {
            "git_commit": local("git", "rev-parse", "HEAD"),
            "git_dirty_files": len(
                [
                    line
                    for line in local("git", "status", "--porcelain").splitlines()
                    if not line.startswith("??")
                ]
            ),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "docker_host": dict(line.split("=", 1) for line in host.splitlines() if "=" in line),
        }

    def check(self) -> dict[str, bool]:
        """Report which host tools the lab needs are present."""
        tools = {}
        for name, command in {
            "docker": "docker version --format '{{.Server.Version}}'",
            "nsenter": "command -v nsenter",
            "iptables": "command -v iptables",
            "tc": "command -v tc",
            "netem": "modprobe sch_netem 2>/dev/null; modinfo sch_netem >/dev/null 2>&1 "
            "|| zcat /proc/config.gz 2>/dev/null | grep -q 'CONFIG_NET_SCH_NETEM=y'",
        }.items():
            tools[name] = not self.sh(command, root=True, check=False).returncode
        try:
            self.compose("version")
            tools["compose"] = True
        except RuntimeError:
            tools["compose"] = False
        return tools


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--wsl", help="WSL distribution holding Docker (Windows only; default distro if unset)"
    )
    parser.add_argument("--compose", help="Compose binary on the Docker host")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    up = commands.add_parser("up")
    up.add_argument("--variant", choices=list(VARIANTS), default="xds", help="Caller and ingress policy")
    up.add_argument("--build", action="store_true")
    commands.add_parser("down")
    commands.add_parser("status")
    commands.add_parser("heal")
    commands.add_parser("hold")
    for name in ("stop", "start"):
        commands.add_parser(name).add_argument("services", nargs="+")
    for name in ("stop-node", "start-node"):
        commands.add_parser(name).add_argument("node", choices=NODES)
    cut = commands.add_parser("partition")
    cut.add_argument("--a", nargs="+", choices=NODES, required=True)
    cut.add_argument("--b", nargs="+", choices=NODES, required=True)
    slow = commands.add_parser("degrade")
    slow.add_argument("node", choices=NODES)
    slow.add_argument("--delay-ms", type=int, default=0)
    slow.add_argument("--loss-percent", type=float, default=0)
    args = parser.parse_args()
    lab = ComposeLab(wsl=args.wsl, compose=args.compose)
    if args.command == "check":
        tools = lab.check()
        print(json.dumps(tools, indent=2))
        sys.exit(0 if all(value for key, value in tools.items() if key != "netem") else 1)
    elif args.command == "up":
        lab.up_variant(args.variant, build=args.build)
    elif args.command == "down":
        lab.down()
    elif args.command == "status":
        for row in lab.containers():
            state = "running" if row["running"] else "stopped"
            print(f"{row['node']:6} {row['service']:16} {state:8} {','.join(row['ips'])}")
    elif args.command == "heal":
        lab.heal()
    elif args.command == "hold":
        with lab.keepalive():
            print("Giữ WSL và lab hoạt động. Nhấn Ctrl+C để thôi giữ.", flush=True)
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                pass
    elif args.command in ("stop", "start"):
        getattr(lab, args.command)(*args.services)
    elif args.command == "stop-node":
        lab.stop_node(args.node)
    elif args.command == "start-node":
        lab.start_node(args.node)
    elif args.command == "partition":
        lab.partition(args.a, args.b)
    elif args.command == "degrade":
        lab.degrade(args.node, delay_ms=args.delay_ms, loss_percent=args.loss_percent)


if __name__ == "__main__":
    main()
