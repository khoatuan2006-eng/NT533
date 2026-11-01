"""Real native gRPC xDS acceptance; isolated Compose project, mutable test config."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import subprocess
import time

from acceptance_microservices import ROOT, execute, linux_path, traffic, wait_graph
from check_microservices import expectations

from grpc_lb.topology import load_topology


async def run(args):
    results = ROOT / "results"
    results.mkdir(exist_ok=True)
    log = results / "lookaside-compose.log"
    report_path = results / "lookaside-acceptance.json"
    runtime = results / "lookaside-discovery.json"
    override = results / "lookaside-compose.override.json"
    config = json.loads((ROOT / "configs/discovery.compose.json").read_text())
    report = {
        "controlplane_runtime": "Python grpc.aio / xds-protos 1.84.0",
        "scope": "single-host Docker, native Python grpcio 1.84.0 xDS, Envoy ingress",
        "started_at_unix": time.time(),
        "passed": False,
        "stages": {},
    }
    stages = report["stages"]
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
        "loadbalance-lookaside-test",
        "-f",
        path(ROOT / "deploy/compose.microservices.yaml"),
        "-f",
        path(ROOT / "deploy/compose.lookaside.yaml"),
    ]
    if (await execute(command, log, "ps", "--all", "--quiet")).strip():
        raise RuntimeError("Test project already exists; stop it explicitly before running")
    runtime.write_text(json.dumps(config), encoding="utf-8")
    override.write_text(
        json.dumps(
            {
                "services": {
                    "controlplane": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": path(runtime),
                                "target": "/config/discovery.json",
                                "read_only": True,
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    command += ["-f", path(override)]
    entry, topology = load_topology(ROOT / "configs/topology.local.json")
    expected = expectations(entry, topology)

    async def graph(label, values=None):
        stages[label] = await wait_graph(args.address, entry, topology, values or expected)
        print(f"{label}: passed", flush=True)

    def update(value):
        # Rewrite the mounted inode; atomic rename of a singly bound file would
        # leave the container reading the old inode. Parser rejects partial writes.
        runtime.write_text(json.dumps(value), encoding="utf-8")

    async def identities():
        output = await execute(command, log, "ps", "--format", "json", "a-1", "a-2")
        rows = (
            json.loads(output)
            if output.lstrip().startswith("[")
            else [json.loads(line) for line in output.splitlines() if line.startswith("{")]
        )
        if {row["Service"] for row in rows} != {"a-1", "a-2"}:
            raise RuntimeError("Missing caller containers")
        found = {}
        for row in rows:
            service = row["Service"]
            ticks = await execute(
                command,
                log,
                "exec",
                "-T",
                service,
                "python",
                "-c",
                "from pathlib import Path; print(Path('/proc/1/stat').read_text().split()[21])",
            )
            found[service] = {"container_id": row["ID"], "process_start_ticks": ticks.strip()}
        return found

    try:
        await execute(command, log, "config", "--quiet")
        print("Building workload and Python xDS control plane", flush=True)
        await execute(command, log, "build", "b-1", "controlplane", timeout=1800)
        await execute(command, log, "up", "--no-build", "-d")
        await graph("initial_graph")
        before = await identities()
        for label, count, concurrency, workload in (
            ("baseline", 1000, 32, "light"),
            ("heavy", 300, 16, "heavy"),
            ("stress", 5000, 64, "light"),
        ):
            result = await traffic(args.address, entry, topology, count, concurrency, workload)
            stages[label] = result
            if result["codes"] != {"OK": count} or result["successful_after_client_timeout"]:
                raise RuntimeError(f"Load acceptance failed: {label}")
            print(f"{label}: {result['codes']}", flush=True)

        reduced = copy.deepcopy(expected)
        reduced["service-b"] = {"b-1"}
        removed = copy.deepcopy(config)
        removed["services"]["service-b"] = ["b-1:50051"]
        update(removed)
        await graph("dynamic_remove_b2", reduced)
        update(config)
        await graph("dynamic_add_b2")
        if before != await identities():
            raise RuntimeError("Callers restarted during dynamic discovery test")
        stages["callers_unchanged"] = before

        update({"schema_version": 999, "services": {}})
        await asyncio.sleep(2)
        await graph("invalid_config_keeps_last_good")
        update(config)
        await asyncio.sleep(2)

        empty = copy.deepcopy(config)
        empty["services"]["service-b"] = []
        update(empty)
        # Wait for convergence before sampling errors; no static fallback allowed.
        await asyncio.sleep(3)
        result = await traffic(args.address, entry, topology, 40, 4, warmup=False)
        stages["empty_membership"] = result
        if result["codes"] != {"UNAVAILABLE": 40}:
            raise RuntimeError("Empty EDS did not fail closed")
        update(config)
        await graph("empty_membership_recovery")

        await execute(command, log, "stop", "b-2")
        await graph("dead_backend_transport_failover", reduced)
        await execute(command, log, "start", "b-2")
        await graph("backend_transport_recovery")

        await execute(command, log, "stop", "controlplane")
        await graph("controlplane_down_cached_config")
        output = await execute(
            command,
            log,
            "run",
            "--rm",
            "--no-deps",
            "-v",
            f"{path(ROOT / 'scripts')}:/app/scripts:ro",
            "a-1",
            "python",
            "/app/scripts/check_xds_cold.py",
        )
        stages["cold_caller_without_controlplane"] = json.loads(
            next(line for line in reversed(output.splitlines()) if line.startswith('{"code"'))
        )
        update(removed)
        await execute(command, log, "start", "controlplane")
        await graph("controlplane_reconnect_new_snapshot", reduced)
        update(config)
        await graph("final_recovery")

        stages["short_deadlines"] = await traffic(
            args.address, entry, topology, 100, 8, timeout=0.001, warmup=False
        )
        if stages["short_deadlines"]["codes"] != {"DEADLINE_EXCEEDED": 100}:
            raise RuntimeError("Unexpected short deadline result")
        await graph("after_deadline_recovery")
        cp_logs = await execute(command, log, "logs", "--no-color", "controlplane")
        (results / "lookaside-controlplane.log").write_text(cp_logs, encoding="utf-8")
        if "xds_ack" not in cp_logs or "snapshot_rejected" not in cp_logs or "xds_nack" in cp_logs:
            raise RuntimeError("Missing ACK/config rejection evidence, or native SDK NACK")
        stages["xds_protocol"] = {
            "ack_observed": True,
            "invalid_config_rejected": True,
            "nack_observed": False,
        }
        report["passed"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        try:
            await execute(command, log, "logs", "--no-color", "--tail", "100", "controlplane", "a-1")
        except Exception:
            pass
        raise
    finally:
        try:
            await execute(command, log, "down", "--remove-orphans", "--volumes")
            report["cleanup"] = "passed"
        except Exception as exc:
            report["cleanup"] = str(exc)
            report["passed"] = False
        report["finished_at_unix"] = time.time()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {report_path}", flush=True)
    if not report["passed"]:
        raise RuntimeError("Lookaside acceptance failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsl")
    parser.add_argument("--compose-binary")
    parser.add_argument("--address", default="localhost:50050")
    args = parser.parse_args()
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
