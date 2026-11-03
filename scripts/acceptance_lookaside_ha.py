"""Registry/health/xDS HA acceptance on a real isolated Consul quorum and services."""

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
    output = ROOT / "results"
    output.mkdir(exist_ok=True)
    log = output / "lookaside-ha-compose.log"
    report_path = output / "lookaside-ha-acceptance.json"
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
        "loadbalance-lookaside-ha-test",
        "-f",
        path(ROOT / "deploy/compose.microservices.yaml"),
        "-f",
        path(ROOT / "deploy/compose.lookaside-ha.yaml"),
    ]
    if (await execute(command, log, "ps", "--all", "--quiet")).strip():
        raise RuntimeError("HA test project already exists; stop it explicitly before running")
    # Only the new instance receives this topology. Existing callers keep their
    # original config and must discover the previously unknown instance via xDS.
    extended = json.loads((ROOT / "configs/topology.compose.json").read_text())
    extended["services"]["service-b"]["instances"]["b-3"] = "b-3:50051"
    extended_path = output / "lookaside-ha-scale-topology.json"
    extended_path.write_text(json.dumps(extended), encoding="utf-8")
    override = output / "lookaside-ha-compose.override.json"
    override.write_text(
        json.dumps(
            {
                "services": {
                    "b-3": {
                        "profiles": ["scale-test"],
                        "image": "loadbalance-workload:local",
                        "command": ["python", "-m", "grpc_lb.microservices"],
                        "environment": {
                            "SERVICE_ID": "service-b",
                            "INSTANCE_ID": "b-3",
                            "LISTEN_ADDRESS": "[::]:50051",
                            "TOPOLOGY_PATH": "/app/configs/topology.json",
                            "CONSUL_HTTP_ADDR": "http://consul-agent:8500",
                            "DRAIN_FILE": "/tmp/drain",
                        },
                        "volumes": [
                            {
                                "type": "bind",
                                "source": path(extended_path),
                                "target": "/app/configs/topology.json",
                                "read_only": True,
                            }
                        ],
                        "depends_on": ["consul-agent"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    command += ["-f", path(override)]
    entry, topology = load_topology(ROOT / "configs/topology.local.json")
    topology["service-b"].instances["b-3"] = "b-3:50051"
    expected = expectations(entry, topology)
    expected["service-b"].remove("b-3")
    reduced = copy.deepcopy(expected)
    reduced["service-b"] = {"b-1"}
    report = {
        "controlplane_runtime": "Python grpc.aio / xds-protos 1.84.0",
        "scope": "single-host Linux containers, Consul 2.1.0 quorum=3, xDS replicas=2",
        "started_at_unix": time.time(),
        "passed": False,
        "stages": {},
    }
    stages = report["stages"]

    async def compose(*args, **kwargs):
        return await execute(command, log, *args, **kwargs)

    async def graph(label, values=None):
        start = time.monotonic()
        counts = await wait_graph(args.address, entry, topology, values or expected)
        stages[label] = {"counts": counts, "converged_in_s": round(time.monotonic() - start, 3)}
        print(f"{label}: passed", flush=True)

    async def drain(instance, enabled):
        code = "from pathlib import Path; p=Path('/tmp/drain'); " + (
            "p.touch()" if enabled else "p.unlink(missing_ok=True)"
        )
        await compose("exec", "-T", instance, "python", "-c", code)

    async def agent_api(endpoint):
        code = f"from urllib.request import urlopen; print(urlopen('http://consul-agent:8500{endpoint}', timeout=3).read().decode())"
        return json.loads(await compose("exec", "-T", "b-1", "python", "-c", code))

    async def fresh(label):
        raw = await compose(
            "run",
            "--rm",
            "--no-deps",
            "-v",
            f"{path(ROOT / 'scripts')}:/app/scripts:ro",
            "a-1",
            "python",
            "/app/scripts/probe_xds.py",
        )
        result = json.loads(
            next(line for line in reversed(raw.splitlines()) if line.startswith('{"instances"'))
        )
        if set(result["instances"]) != {"b-1", "b-2"}:
            raise RuntimeError("Fresh caller did not discover both replicas")
        stages[label] = result

    async def identities():
        result = {}
        for service in ("a-1", "a-2"):
            result[service] = (
                await compose(
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    "from pathlib import Path; print(Path('/proc/1/stat').read_text().split()[21])",
                )
            ).strip()
        return result

    try:
        await compose("config", "--quiet")
        print("Building and starting Consul quorum / xDS replicas", flush=True)
        await compose("build", "b-1", "controlplane-1", timeout=1800)
        await compose("up", "--no-build", "-d")
        await graph("initial_registration_and_health")
        stages["controlplane_image_runtime"] = json.loads(
            await compose(
                "exec",
                "-T",
                "controlplane-1",
                "python",
                "-c",
                "import importlib.util, json, platform; from importlib.metadata import version; "
                "import grpc_lb.controlplane; print(json.dumps({'python': platform.python_version(), "
                "'xds_protos': version('xds-protos'), 'module': grpc_lb.controlplane.__file__, "
                "'has_pytest': importlib.util.find_spec('pytest') is not None}))",
            )
        )
        if stages["controlplane_image_runtime"]["has_pytest"]:
            raise RuntimeError("Control plane runtime image contains development test dependencies")
        before = await identities()
        peers = await agent_api("/v1/operator/raft/configuration")
        if len(peers["Servers"]) != 3 or sum(s["Leader"] for s in peers["Servers"]) != 1:
            raise RuntimeError("Registry did not form a three-server quorum")
        stages["initial_raft_peers"] = peers

        for label, count, concurrency, workload in (
            ("baseline", 1000, 32, "light"),
            ("heavy", 300, 16, "heavy"),
            ("stress", 5000, 64, "light"),
        ):
            result = await traffic(args.address, entry, topology, count, concurrency, workload)
            stages[label] = result
            if result["codes"] != {"OK": count} or result["successful_after_client_timeout"]:
                raise RuntimeError(f"Load/deadline failure: {label}")
            print(f"{label}: {result['codes']}", flush=True)

        await drain("b-2", True)
        await graph("not_serving_withdraws_from_eds", reduced)
        code = (
            "import grpc,json; from grpc_health.v1 import health_pb2 as h,health_pb2_grpc as hr; "
            "from grpc_lb.generated import processing_pb2 as p,processing_pb2_grpc as pr; "
            "c=grpc.insecure_channel('localhost:50051'); "
            "s=hr.HealthStub(c).Check(h.HealthCheckRequest(service='processing.v1.ProcessingService/service-b'),timeout=2).status; "
            "r=pr.ProcessingServiceStub(c).Execute(p.TaskRequest(request_id='drained-alive',target_service='service-b',workload='light',remaining_hops=64),timeout=2); "
            "print(json.dumps({'health':s,'business_instance':r.instance_id}))"
        )
        stages["drained_backend_transport_alive"] = json.loads(
            await compose("exec", "-T", "b-2", "python", "-c", code)
        )
        if stages["drained_backend_transport_alive"] != {"health": 2, "business_instance": "b-2"}:
            raise RuntimeError("Drain test did not distinguish health from connectivity")
        await drain("b-2", False)
        await graph("serving_readds_to_eds")

        await compose("up", "--no-build", "-d", "b-3")
        scaled = copy.deepcopy(expected)
        scaled["service-b"].add("b-3")
        await graph("new_instance_self_registers", scaled)
        await compose("stop", "b-3")
        await graph("graceful_deregister")

        await compose("stop", "controlplane-1")
        await drain("b-2", True)
        await graph("cp2_delivers_new_health_update", reduced)
        await drain("b-2", False)
        await graph("cp2_delivers_recovery")
        await fresh("cold_caller_with_only_cp2")
        await compose("start", "controlplane-1")
        await compose("stop", "controlplane-2")
        await drain("b-2", True)
        await graph("cp1_delivers_new_health_update", reduced)
        await drain("b-2", False)
        await graph("cp1_delivers_recovery")
        await fresh("cold_caller_with_only_cp1")
        await compose("start", "controlplane-2")

        peers = await agent_api("/v1/operator/raft/configuration")
        leader = next(s["Node"] for s in peers["Servers"] if s["Leader"])
        await compose("pause", leader)
        await drain("b-2", True)
        await graph("registry_leader_loss_health_update", reduced)
        if stages["registry_leader_loss_health_update"]["converged_in_s"] > 10:
            raise RuntimeError("Leader loss exceeded the 10-second health convergence target")
        peers = await agent_api("/v1/operator/raft/configuration")
        if next(s["Node"] for s in peers["Servers"] if s["Leader"]) == leader:
            raise RuntimeError("Registry did not elect a replacement leader")
        stages["replacement_raft_peers"] = peers
        await compose("unpause", leader)
        await drain("b-2", False)
        await graph("registry_leader_recovery")

        # Consul Autopilot can remove a failed leader from the voting set.
        # Wait for its rejoin/promotion before proving loss of two out of three.
        due = time.monotonic() + 45
        while True:
            peers = await agent_api("/v1/operator/raft/configuration")
            if len(peers["Servers"]) == 3 and all(s["Voter"] for s in peers["Servers"]):
                stages["registry_voters_restored"] = peers
                break
            if time.monotonic() >= due:
                raise RuntimeError("Recovered registry server did not rejoin the three-voter quorum")
            await asyncio.sleep(1)

        await compose("pause", "consul-2", "consul-3")
        await asyncio.sleep(22)
        result = await traffic(args.address, entry, topology, 40, 4, warmup=False)
        stages["registry_quorum_loss_expires_membership"] = result
        if result["codes"] != {"UNAVAILABLE": 40}:
            raise RuntimeError("Registry quorum loss kept expired membership")
        await compose("unpause", "consul-2", "consul-3")
        await graph("registry_quorum_recovery")
        await compose("restart", "consul-agent")
        await graph("agent_restart_registration_recovery")

        await compose("stop", "b-2")
        await graph("backend_stop", reduced)
        await compose("start", "b-2")
        await graph("backend_reregistration")
        if before != await identities():
            raise RuntimeError("Existing callers restarted during registry/HA updates")
        stages["caller_start_ticks_unchanged"] = before
        stages["short_deadlines"] = await traffic(
            args.address, entry, topology, 100, 8, timeout=0.001, warmup=False
        )
        if stages["short_deadlines"]["codes"] != {"DEADLINE_EXCEEDED": 100}:
            raise RuntimeError("Unexpected short deadline result")
        await graph("final_recovery")
        cp_logs = await compose("logs", "--no-color", "controlplane-1", "controlplane-2")
        (output / "lookaside-ha-controlplane.log").write_text(cp_logs, encoding="utf-8")
        if "xds_nack" in cp_logs or "registry_lease_expired" not in cp_logs or "xds_ack" not in cp_logs:
            raise RuntimeError("Missing xDS/lease evidence or unexpected NACK")
        report["passed"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        try:
            await compose(
                "logs",
                "--no-color",
                "--tail",
                "100",
                "controlplane-1",
                "controlplane-2",
                "consul-agent",
                "a-1",
            )
        except Exception:
            pass
        raise
    finally:
        # A failed fault test may leave servers paused. Always thaw before cleanup.
        try:
            await compose("unpause", "consul-1", "consul-2", "consul-3")
        except Exception:
            pass
        try:
            await execute([*command, "--profile", "scale-test"], log, "down", "--remove-orphans", "--volumes")
            if (await compose("ps", "--all", "--quiet")).strip():
                raise RuntimeError("Test containers remain after cleanup")
            report["cleanup"] = "passed"
        except Exception as exc:
            report["cleanup"] = str(exc)
            report["passed"] = False
        report["finished_at_unix"] = time.time()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report: {report_path}", flush=True)
    if not report["passed"]:
        raise RuntimeError("HA acceptance or cleanup failed")


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
