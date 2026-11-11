"""Reproducible leader-freeze experiment with Consul config A/B and live traffic."""

import argparse
import asyncio
import json
import subprocess
import time

from acceptance_microservices import ROOT, execute, linux_path, wait_graph
from check_microservices import expectations

from grpc_lb.topology import load_topology


async def run(args):
    folder = ROOT / "results" / f"ha-{args.label}"
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "compose.log"
    path = linux_path if args.wsl else str
    command = (
        ["wsl", "-d", args.wsl, "--", args.compose_binary or "docker-compose"]
        if args.wsl
        else [args.compose_binary]
        if args.compose_binary
        else ["docker", "compose"]
    )
    command += [
        "--project-directory",
        path(ROOT / "deploy"),
        "-p",
        "loadbalance-ha-probe",
        "-f",
        path(ROOT / "deploy/compose.microservices.yaml"),
        "-f",
        path(ROOT / "deploy/compose.lookaside-ha.yaml"),
    ]
    config = {
        "performance": {"raft_multiplier": args.raft_multiplier},
        "limits": {"rpc_client_timeout": args.rpc_timeout},
    }
    config_path = folder / "consul.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    overrides = {
        name: {
            "volumes": [
                {
                    "type": "bind",
                    "source": path(config_path),
                    "target": "/consul/config/ha-profile.json",
                    "read_only": True,
                }
            ]
        }
        for name in ("consul-1", "consul-2", "consul-3", "consul-agent")
    }
    overrides["b-1"] = {
        "volumes": [
            {"type": "bind", "source": path(ROOT / "scripts"), "target": "/app/scripts", "read_only": True}
        ]
    }
    for cp in ("controlplane-1", "controlplane-2"):
        overrides[cp] = {
            "environment": {"CONSUL_ACTIVE_HEALTH": "1" if args.controller_mode == "active" else "0"}
        }
    override = folder / "compose.override.json"
    override.write_text(json.dumps({"services": overrides}), encoding="utf-8")
    command += ["-f", path(override)]

    async def compose(*cmd):
        return await execute(command, log, *cmd)

    if (await compose("ps", "--all", "--quiet")).strip():
        raise RuntimeError("Probe project exists; stop it explicitly before running")
    entry, topology = load_topology(ROOT / "configs/topology.local.json")
    expected = expectations(entry, topology)
    reduced = {**expected, "service-b": {"b-1"}}
    report = {"profile": config, "controller_mode": args.controller_mode, "passed": False, "trials": []}
    observer = None

    async def python(code):
        return await compose("exec", "-T", "b-1", "python", "-c", code)

    async def peers():
        return json.loads(
            await python(
                "from urllib.request import urlopen; print(urlopen('http://consul-agent:8500/v1/operator/raft/configuration',timeout=3).read().decode())"
            )
        )

    try:
        await compose("up", "--no-build", "-d")
        await wait_graph("localhost:50050", entry, topology, expected)
        effective = await python(
            "import json; from urllib.request import urlopen; d=json.load(urlopen('http://consul-agent:8500/v1/agent/self'))['DebugConfig']; print(json.dumps({k:v for k,v in d.items() if k in ('RaftMultiplier','RPCClientTimeout')}))"
        )
        report["agent_effective_config"] = json.loads(effective)
        for index in range(args.repetitions):
            due = time.monotonic() + 45
            while True:
                p = await peers()
                if len(p["Servers"]) == 3 and all(v["Voter"] for v in p["Servers"]):
                    break
                if time.monotonic() > due:
                    raise RuntimeError("Three voters did not become ready")
                await asyncio.sleep(1)
            leader = next(v["Node"] for v in p["Servers"] if v["Leader"])
            trace_log = folder / f"trial-{index}-observer.log"
            trace_log.write_text("", encoding="utf-8")
            observer = asyncio.create_task(
                execute(
                    command,
                    trace_log,
                    "exec",
                    "-T",
                    "b-1",
                    "python",
                    "-u",
                    "/app/scripts/probe_ha_timeline.py",
                )
            )
            due = time.monotonic() + 15
            while "observer_ready" not in trace_log.read_text(encoding="utf-8"):
                if observer.done():
                    await observer
                    raise RuntimeError("Observer stopped before ready")
                if time.monotonic() > due:
                    raise RuntimeError("Observer did not become ready")
                await asyncio.sleep(0.1)
            await asyncio.sleep(1)
            fault_started = time.time()
            hold_start = time.monotonic()
            await compose("pause", leader)
            await compose(
                "exec", "-T", "b-2", "python", "-c", "from pathlib import Path; Path('/tmp/drain').touch()"
            )
            start = time.monotonic()
            await wait_graph("localhost:50050", entry, topology, reduced)
            converged = time.monotonic() - start
            await asyncio.sleep(max(0, 45 - (time.monotonic() - hold_start)))
            await compose("unpause", leader)
            await compose(
                "exec",
                "-T",
                "b-2",
                "python",
                "-c",
                "from pathlib import Path; Path('/tmp/drain').unlink(missing_ok=True)",
            )
            await wait_graph("localhost:50050", entry, topology, expected)
            await python("from pathlib import Path; Path('/tmp/ha-observer-stop').touch()")
            raw = await observer
            observer = None
            trace = json.loads(
                next(line for line in reversed(raw.splitlines()) if line.startswith('{"started_unix_s"'))
            )
            (folder / f"trial-{index}-timeline.json").write_text(
                json.dumps(trace, indent=2), encoding="utf-8"
            )
            trial = {
                "leader": leader,
                "fault_started_unix_s": fault_started,
                "health_convergence_s": round(converged, 3),
                "codes": trace["codes"],
                "late_ok": trace["late_ok"],
            }
            report["trials"].append(trial)
            print(json.dumps(trial), flush=True)
            if args.controller_mode == "active" and (
                converged > 10
                or trace["late_ok"]
                or set(trace["codes"]) != {"OK"}
                or trace["codes"].get("OK", 0) < 500
            ):
                raise RuntimeError(
                    "Active HA gate failed: <=10s convergence, >=500 live OK RPCs, no errors or late OK required"
                )
        report["passed"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        if observer:
            try:
                await python("from pathlib import Path; Path('/tmp/ha-observer-stop').touch()")
                await asyncio.wait_for(observer, 10)
            except Exception:
                observer.cancel()
        try:
            await compose("unpause", "consul-1", "consul-2", "consul-3")
        except Exception:
            pass
        try:
            await compose(
                "logs",
                "--no-color",
                "--timestamps",
                "consul-1",
                "consul-2",
                "consul-3",
                "consul-agent",
                "controlplane-1",
                "controlplane-2",
            )
        except Exception as exc:
            report["passed"] = False
            report["diagnostic_log_error"] = str(exc)
            raise
        finally:
            try:
                await compose("down", "--remove-orphans", "--volumes")
                if (await compose("ps", "--all", "--quiet")).strip():
                    raise RuntimeError("Probe project cleanup incomplete")
                report["cleanup"] = "passed"
            except BaseException as exc:
                report["passed"] = False
                report["cleanup"] = str(exc)
                raise
            finally:
                (folder / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wsl")
    p.add_argument("--compose-binary")
    p.add_argument("--label", required=True)
    p.add_argument("--raft-multiplier", type=int, default=5)
    p.add_argument("--rpc-timeout", default="60s")
    p.add_argument("--repetitions", type=int, default=1)
    p.add_argument("--controller-mode", choices=["catalog", "active"], default="catalog")
    args = p.parse_args()
    if args.label not in ("baseline", "rpc2", "fast", "active") or not 1 <= args.repetitions <= 5:
        p.error("Use baseline/rpc2/fast/active labels and 1..5 repetitions")
    keepalive = None
    if args.wsl:
        keepalive = subprocess.Popen(
            ["wsl", "-d", args.wsl, "--", "cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        asyncio.run(run(args))
    finally:
        if keepalive:
            keepalive.stdin.close()
            keepalive.wait(timeout=10)


if __name__ == "__main__":
    main()
