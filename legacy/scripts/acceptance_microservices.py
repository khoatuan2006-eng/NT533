"""Reproducible single-host acceptance through a real Envoy/Compose stack.

Creates an isolated project, exercises faults/load, writes JSON, cleans up.
Requires Docker and Compose. Windows can use --wsl and --compose-binary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
from collections import Counter
from pathlib import Path
from urllib.request import urlopen

import grpc
from check_microservices import check, expectations

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc
from grpc_lb.topology import load_topology

ROOT = Path(__file__).resolve().parent.parent


def verdict(report):
    late = {
        name: stage.get("successful_after_client_timeout", 0)
        for name, stage in report.get("stages", {}).items()
        if isinstance(stage, dict)
    }
    functional = report.get("passed", False) and report.get("cleanup") == "passed"
    return {
        "functional_checks_passed": functional,
        "late_successes_by_stage": late,
        "client_wall_deadline_accepted": not any(late.values()),
        "overall_accepted": functional and not any(late.values()),
    }


def expected_overload_codes(result):
    """Envoy circuit breaking returns 503/UNAVAILABLE; require its marker."""
    return not (
        set(result["codes"]) - {"OK", "RESOURCE_EXHAUSTED", "DEADLINE_EXCEEDED", "UNAVAILABLE"}
    ) and result["codes"].get("UNAVAILABLE", 0) == result.get("verified_proxy_overflow_rejections", 0)


def linux_path(path: Path) -> str:
    return f"/mnt/{path.drive[0].lower()}/{path.as_posix()[3:]}" if path.drive else str(path)


async def execute(command: list[str], log: Path, *args: str, timeout: float = 600) -> str:
    process = await asyncio.create_subprocess_exec(
        *command, *args, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    chunks = []

    async def collect():
        # Preserve build diagnostics while a long command is still running or
        # times out, rather than writing the entire log only after completion.
        with log.open("ab") as stream:
            stream.write(f"\n$ {args}\n".encode("utf-8"))
            stream.flush()
            while chunk := await process.stdout.read(8192):
                chunks.append(chunk)
                stream.write(chunk)
                stream.flush()
            stream.write(b"\n")
        await process.wait()

    try:
        await asyncio.wait_for(collect(), timeout)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    text = b"".join(chunks).decode("utf-8", errors="replace")
    if process.returncode:
        raise RuntimeError(f"Compose {args} failed ({process.returncode}); see {log}")
    return text


async def wait_graph(address, entry, topology, expected):
    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await check(address, entry, topology, expected, 24, 2)
        except (grpc.RpcError, TimeoutError, RuntimeError) as exc:
            last_error = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Graph did not converge: {last_error}")


async def traffic(address, entry, topology, count, concurrency, workload="light", timeout=3, warmup=True):
    codes, latencies, instances = Counter(), [], Counter()
    server_durations, loop_lags, timing_samples = [], [], []
    proxy_rejections = 0
    failure_samples = []
    seen_failures = set()
    issued = 0
    late_successes = 0
    started = time.perf_counter()
    warmup_elapsed = 0.0

    async def observe_loop():
        loop = asyncio.get_running_loop()
        while True:
            due = loop.time() + 0.02
            await asyncio.sleep(0.02)
            loop_lags.append(max(0, loop.time() - due) * 1000)

    observer = asyncio.create_task(observe_loop())

    def verify(result, name, request_id):
        if (
            result.service_id != name
            or result.request_id != request_id
            or result.instance_id not in topology[name].instances
        ):
            raise RuntimeError("Invalid RPC result identity")
        instances[result.instance_id] += 1
        if [c.service_id for c in result.children] != list(topology[name].dependencies):
            raise RuntimeError("Invalid RPC result graph")
        for child in result.children:
            verify(child, child.service_id, request_id)

    try:
        async with grpc.aio.insecure_channel(address) as channel:
            stub = rpc.ProcessingServiceStub(channel)
            await asyncio.wait_for(channel.channel_ready(), 10)
            # Warm this exact HTTP/2 connection and its Envoy worker/upstream
            # pools before measuring steady-state. A fresh channel can legitimately
            # overflow the configured pending limit while connections are opening.
            for index in range(4 if warmup else 0):
                await stub.Execute(
                    pb.TaskRequest(
                        request_id=f"warmup-{index}",
                        target_service=entry,
                        workload="light",
                        remaining_hops=64,
                    ),
                    timeout=3,
                )
            warmup_elapsed = time.perf_counter() - started
            loop_lags.clear()
            started = time.perf_counter()

            async def worker():
                nonlocal issued, late_successes, proxy_rejections
                while issued < count:
                    index = issued
                    issued += 1
                    request_id = f"acceptance-{index}"
                    start = time.perf_counter()
                    try:
                        call = stub.Execute(
                            pb.TaskRequest(
                                request_id=request_id,
                                target_service=entry,
                                workload=workload,
                                remaining_hops=64,
                            ),
                            timeout=timeout,
                        )
                        result = await call
                        # Measure receipt immediately, before validation/aggregation.
                        latency_ms = (time.perf_counter() - start) * 1000
                        trailers = dict(list(await call.trailing_metadata()))
                        if "x-server-duration-ms" in trailers:
                            server_durations.append(float(trailers["x-server-duration-ms"]))
                        sample = {
                            "request_id": request_id,
                            "client_elapsed_ms": round(latency_ms, 3),
                            "timeout_ms": timeout * 1000,
                            "server_trailers": trailers,
                        }
                        if (latency_ms > timeout * 1000 or index < 8) and len(timing_samples) < 32:
                            timing_samples.append(sample)
                        verify(result, entry, request_id)
                        codes["OK"] += 1
                        latencies.append(latency_ms)
                        if latency_ms > timeout * 1000:
                            late_successes += 1
                    except grpc.aio.AioRpcError as exc:
                        codes[exc.code().name] += 1
                        metadata = dict(list(exc.initial_metadata() or ()))
                        metadata.update(dict(list(exc.trailing_metadata() or ())))
                        failure_key = (exc.code().name, exc.details())
                        if failure_key not in seen_failures and len(failure_samples) < 16:
                            failure_samples.append(
                                {"code": exc.code().name, "details": exc.details(), "metadata": metadata}
                            )
                            seen_failures.add(failure_key)
                        if exc.code() == grpc.StatusCode.UNAVAILABLE and "x-envoy-overloaded" in metadata:
                            proxy_rejections += 1

            await asyncio.gather(*(worker() for _ in range(concurrency)))
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    elapsed = time.perf_counter() - started
    latencies.sort()

    def percentile(p):
        return round(latencies[max(0, math.ceil(len(latencies) * p) - 1)], 3) if latencies else None

    return {
        "requests": count,
        "concurrency": concurrency,
        "workload": workload,
        "warmup_requests": 4 if warmup else 0,
        "warmup_elapsed_s": round(warmup_elapsed, 3),
        "elapsed_s": round(elapsed, 3),
        "codes": dict(codes),
        "successful_rps": round(codes["OK"] / elapsed, 2),
        "successful_after_client_timeout": late_successes,
        "verified_proxy_overflow_rejections": proxy_rejections,
        "max_client_event_loop_lag_ms": round(max(loop_lags, default=0), 3),
        "max_successful_server_duration_ms": max(server_durations, default=None),
        "timing_samples": timing_samples,
        "failure_samples": failure_samples,
        "success_latency_ms": {"p50": percentile(0.5), "p95": percentile(0.95), "p99": percentile(0.99)},
        "instance_visits": dict(instances),
    }


async def run(args):
    report_path = ROOT / "results" / "acceptance.json"
    log = ROOT / "results" / "acceptance-compose.log"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    config = ROOT / "deploy" / "compose.microservices.yaml"
    runtime_path = ROOT / "results" / "acceptance-topology.json"
    override_path = ROOT / "results" / "acceptance-compose.override.json"
    runtime_data = json.loads((ROOT / "configs" / "topology.compose.json").read_text(encoding="utf-8"))
    mount_source = linux_path(runtime_path) if args.wsl else str(runtime_path)
    override = {
        "services": {
            instance: {
                "volumes": [
                    {
                        "type": "bind",
                        "source": mount_source,
                        "target": "/app/configs/topology.json",
                        "read_only": True,
                    }
                ]
            }
            for cfg in runtime_data["services"].values()
            for instance in cfg["instances"]
        }
    }
    if args.wsl:
        command = [
            "wsl",
            "-d",
            args.wsl,
            "--",
            args.compose_binary or "docker-compose",
            "--project-directory",
            linux_path(config.parent),
            "-p",
            "loadbalance-acceptance",
            "-f",
            linux_path(config),
        ]
    else:
        command = [args.compose_binary] if args.compose_binary else ["docker", "compose"]
        command += ["-p", "loadbalance-acceptance", "-f", str(config)]
    entry, topology = load_topology(ROOT / "configs" / "topology.local.json")
    expected = expectations(entry, topology)
    report = {
        "scope": "single-host Docker/Envoy, synthetic asynchronous-delay workload",
        "client_environment": {
            "grpc": grpc.__version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "started_at_unix": time.time(),
        "stages": {},
        "passed": False,
    }
    # Do not take over or remove someone else's running acceptance project.
    if (await execute(command, log, "ps", "--all", "--quiet")).strip():
        raise RuntimeError("Acceptance project already has containers; stop it explicitly before running")
    runtime_path.write_text(json.dumps(runtime_data), encoding="utf-8")
    override_path.write_text(json.dumps(override), encoding="utf-8")
    command += ["-f", linux_path(override_path) if args.wsl else str(override_path)]
    try:
        await execute(command, log, "config", "--quiet")
        await execute(
            command,
            log,
            "run",
            "--rm",
            "--no-deps",
            "ingress",
            "--mode",
            "validate",
            "-c",
            "/etc/envoy/envoy.yaml",
        )
        report["stages"]["envoy_config_validation"] = "passed"
        print("Envoy config validated; starting stack", flush=True)
        # All workload instances use one image. Build once, then start replicas.
        await execute(command, log, "build", "b-1")
        await execute(command, log, "up", "--no-build", "-d")
        report["stages"]["initial_graph"] = await wait_graph(args.address, entry, topology, expected)
        for name, count, concurrency, workload in (
            ("baseline", 1000, 32, "light"),
            ("heavy", 300, 16, "heavy"),
            ("bounded_stress", 5000, 64, "light"),
        ):
            result = await traffic(args.address, entry, topology, count, concurrency, workload)
            report["stages"][name] = result
            if result["codes"] != {"OK": count}:
                raise RuntimeError(f"Unexpected errors in {name}: {result['codes']}")
            print(f"{name}: {result['codes']}, {result['successful_rps']} successful RPC/s", flush=True)

        # Force a known admission limit; client concurrency alone does not prove
        # backend saturation because proxies/HTTP2 can queue the requests first.
        normal_concurrency = runtime_data["services"][entry].get("concurrency", 50)
        runtime_data["services"][entry]["concurrency"] = 2
        runtime_path.write_text(json.dumps(runtime_data), encoding="utf-8")
        await execute(command, log, "up", "--no-build", "--force-recreate", "--no-deps", "-d", "a-1", "a-2")
        await wait_graph(args.address, entry, topology, expected)
        overload = await traffic(args.address, entry, topology, 1000, 128, "heavy")
        overload["entry_concurrency_per_instance"] = 2
        report["stages"]["overload"] = overload
        await execute(command, log, "logs", "--no-color", "--tail", "1200", "ingress")
        if not overload["codes"].get("RESOURCE_EXHAUSTED") or overload["codes"].get("OK", 0) == 0:
            raise RuntimeError("Overload did not exercise both shedding and successful work")
        scripts_path = linux_path(ROOT / "scripts") if args.wsl else str(ROOT / "scripts")
        linux_output = await execute(
            command,
            log,
            "run",
            "--rm",
            "--no-deps",
            "-v",
            f"{scripts_path}:/app/scripts:ro",
            "b-1",
            "python",
            "/app/scripts/probe_load.py",
        )
        linux_result = json.loads(
            next(line for line in reversed(linux_output.splitlines()) if line.startswith('{"requests"'))
        )
        report["stages"]["linux_client_overload"] = linux_result
        overload_codes_accepted = expected_overload_codes(linux_result) and expected_overload_codes(overload)
        report["stages"]["overload_codes_accepted"] = overload_codes_accepted
        print(
            f"Linux client overload: {linux_result['codes']}, p99={linux_result['success_latency_ms']['p99']} ms",
            flush=True,
        )
        runtime_data["services"][entry]["concurrency"] = normal_concurrency
        runtime_path.write_text(json.dumps(runtime_data), encoding="utf-8")
        await execute(command, log, "up", "--no-build", "--force-recreate", "--no-deps", "-d", "a-1", "a-2")
        report["stages"]["after_overload"] = await wait_graph(args.address, entry, topology, expected)

        for service, instance, remaining in (("service-b", "b-2", "b-1"), ("service-a", "a-2", "a-1")):
            await execute(command, log, "stop", instance)
            reduced = {s: set(ids) for s, ids in expected.items()}
            reduced[service] = {remaining}
            report["stages"][f"stop_{instance}"] = await wait_graph(args.address, entry, topology, reduced)
            await execute(command, log, "start", instance)
            report["stages"][f"recover_{instance}"] = await wait_graph(
                args.address, entry, topology, expected
            )
            print(f"stop/recover {instance}: passed", flush=True)

        for label, stopped in (
            ("all_dependencies_down", ("b-1", "b-2")),
            ("all_entry_instances_down", ("a-1", "a-2")),
        ):
            await execute(command, log, "stop", *stopped)
            deadline = time.monotonic() + 20
            while True:
                result = await traffic(args.address, entry, topology, 10, 1, warmup=False)
                if result["codes"] == {"UNAVAILABLE": 10}:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{label} did not fail closed: {result['codes']}")
                await asyncio.sleep(0.5)
            report["stages"][label] = result
            await execute(command, log, "start", *stopped)
            report["stages"][f"recover_{label}"] = await wait_graph(args.address, entry, topology, expected)
            print(f"{label}/recovery: passed", flush=True)

        report["stages"]["short_deadlines"] = await traffic(
            args.address, entry, topology, 100, 32, "heavy", 0.001
        )
        if report["stages"]["short_deadlines"]["codes"] != {"DEADLINE_EXCEEDED": 100}:
            raise RuntimeError("Short deadlines were not exercised")
        report["stages"]["after_deadlines"] = await wait_graph(args.address, entry, topology, expected)

        def admin_metrics():
            with urlopen(args.admin_url + "/stats/prometheus", timeout=3) as response:
                return response.read()

        metrics = await asyncio.to_thread(admin_metrics)
        if b"envoy_cluster" not in metrics:
            raise RuntimeError("Missing Envoy metrics")
        (ROOT / "results" / "acceptance-envoy.prom").write_bytes(metrics)
        report["passed"] = overload_codes_accepted
        if not overload_codes_accepted:
            report["error"] = "Unclassified overload RPC failures; remaining fault checks were still executed"
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        try:
            await execute(command, log, "logs", "--no-color", "--tail", "50")
            await execute(command, log, "down", "--remove-orphans", "--volumes")
            report["cleanup"] = "passed"
        except Exception as exc:
            report["cleanup"] = str(exc)
            report["passed"] = False
        report["finished_at_unix"] = time.time()
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        decision = verdict(report)
        report_path.with_name("acceptance-verdict.json").write_text(
            json.dumps(decision, indent=2), encoding="utf-8"
        )
        print(f"Acceptance report: {report_path}", flush=True)
    if not report["passed"]:
        raise RuntimeError("Acceptance or cleanup failed; see report")
    if not decision["overall_accepted"]:
        raise RuntimeError(
            "Functional checks passed, but client wall-clock deadline is not accepted; see acceptance-verdict.json"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsl", help="WSL distro with running Docker Engine")
    parser.add_argument("--compose-binary", help="Standalone Compose executable (Linux path when using WSL)")
    parser.add_argument("--address", default="localhost:50050")
    parser.add_argument("--admin-url", default="http://localhost:9901")
    args = parser.parse_args()

    # systemd daemons/containers alone need not keep a WSL distro running.
    # Hold a foreground session while the Windows client sends traffic.
    async def with_keepalive():
        keepalive = None
        try:
            if args.wsl:
                keepalive = await asyncio.create_subprocess_exec(
                    "wsl",
                    "-d",
                    args.wsl,
                    "--",
                    "cat",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            await run(args)
        finally:
            if keepalive:
                keepalive.stdin.close()
                await asyncio.wait_for(keepalive.wait(), 10)

    try:
        asyncio.run(with_keepalive())
    except (RuntimeError, OSError, grpc.RpcError, TimeoutError) as exc:
        parser.exit(1, f"Acceptance FAILED: {exc}\n")


if __name__ == "__main__":
    main()
