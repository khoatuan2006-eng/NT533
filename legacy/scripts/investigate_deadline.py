"""Run a minimal Windows/Linux x sync/AsyncIO x direct/Envoy matrix.

Uses isolated ports/project and preserves each raw result. No production tuning.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import grpc
from grpc_health.v1 import health_pb2 as hp
from grpc_health.v1 import health_pb2_grpc as hr

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "deadline-lab"


def linux(path):
    return f"/mnt/{path.drive[0].lower()}/{path.as_posix()[3:]}" if path.drive else str(path)


def run(command, timeout=180, env=None):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=timeout, env=env)
    with (OUT / "commands.log").open("ab") as log:
        log.write(("\n$ " + repr(command) + "\n").encode())
        log.write(result.stderr)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {command}; {result.stderr[-1000:]!r}")
    return result.stdout.decode("utf-8", errors="replace")


def ready(address):
    end = time.monotonic() + 30
    with grpc.insecure_channel(address) as channel:
        stub = hr.HealthStub(channel)
        while time.monotonic() < end:
            try:
                if (
                    stub.Check(hp.HealthCheckRequest(service=""), timeout=0.3).status
                    == hp.HealthCheckResponse.SERVING
                ):
                    return
            except grpc.RpcError:
                pass
            time.sleep(0.1)
    raise RuntimeError(f"Probe did not become ready at {address}")


def main():
    global OUT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wsl", default="Ubuntu-22.04")
    parser.add_argument(
        "--compose-binary", default="/mnt/d/LoadBalance/.venv/acceptance-tools/docker-compose-linux-x86_64"
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", default="results/deadline-lab")
    parser.add_argument("--client-grpc-path", help="Isolated grpcio target directory, Windows clients only")
    parser.add_argument(
        "--profiles", nargs="+", choices=["fast", "slow", "burst"], default=["fast", "slow", "burst"]
    )
    parser.add_argument(
        "--native-server-grpc-path", help="Isolated grpcio directory for native Windows server"
    )
    parser.add_argument("--paths", nargs="+", help="Only run these path labels; omitted means all")
    args = parser.parse_args()
    if args.repetitions <= 0:
        parser.error("Positive repetition count required")
    OUT = (ROOT / args.output).resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    config_path = OUT / "compose.json"
    compose = [
        "wsl",
        "-d",
        args.wsl,
        "--",
        args.compose_binary,
        "-p",
        "loadbalance-deadline-lab",
        "-f",
        linux(config_path),
    ]
    config = {
        "services": {
            "probe": {
                "image": "loadbalance-deadline-probe:local",
                "build": {"context": linux(ROOT), "dockerfile": "deploy/Dockerfile"},
                "command": ["python", "/app/scripts/deadline_probe.py", "--serve", "--address", "[::]:50051"],
                "volumes": [f"{linux(ROOT / 'scripts')}:/app/scripts:ro"],
                "ports": ["127.0.0.1:50251:50051"],
            },
            "ingress": {
                "image": "envoyproxy/envoy:v1.35.5",
                "command": ["-c", "/etc/envoy/envoy.yaml", "--concurrency", "2", "--log-level", "warning"],
                "volumes": [f"{linux(OUT / 'envoy.yaml')}:/etc/envoy/envoy.yaml:ro"],
                "ports": ["127.0.0.1:50250:50050", "127.0.0.1:50291:9901"],
                "depends_on": ["probe"],
            },
        }
    }
    source = (ROOT / "deploy" / "envoy.microservices.yaml").read_text(encoding="utf-8")
    source = source.replace(
        "processing.v1.ProcessingService/service-a", "processing.v1.ProcessingService/probe"
    )
    source = source.replace("address: a-1", "address: probe")
    source = source.replace(
        "              - endpoint:\n                  address:\n                    socket_address: {address: a-2, port_value: 50051}\n",
        "",
    )
    source = source.replace("max_requests: 100", "max_requests: 512").replace(
        "max_pending_requests: 32", "max_pending_requests: 256"
    )
    # Isolate transport from per-request logging. The service itself has no log I/O.
    start, end = source.index("                access_log:"), source.index("                http_filters:")
    source = source[:start] + source[end:]
    (OUT / "envoy.yaml").write_text(source, encoding="utf-8")
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    keepalive, native = None, None
    owns_project = False
    report = {"started_at_unix": time.time(), "purpose": "diagnostic matrix, not acceptance", "cases": []}
    try:
        keepalive = subprocess.Popen(
            ["wsl", "-d", args.wsl, "--", "cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if run(compose + ["ps", "--all", "--quiet"]).strip():
            raise RuntimeError("Lab project already has containers; refusing takeover")
        owns_project = True
        with socket.socket() as reservation:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                reservation.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            reservation.bind(("127.0.0.1", 50261))
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        if args.native_server_grpc_path:
            env["PYTHONPATH"] = os.pathsep.join(
                [str((ROOT / args.native_server_grpc_path).resolve()), str(ROOT / "src")]
            )
        native = subprocess.Popen(
            [sys.executable, "scripts/deadline_probe.py", "--serve", "--address", "127.0.0.1:50261"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        ready("127.0.0.1:50261")
        paths = [("windows-native-direct", [sys.executable, "scripts/deadline_probe.py"], "127.0.0.1:50261")]

        # Run native cases first, before introducing Docker/WSL server and Envoy.
        def cases(paths):
            for path, base, address in paths:
                if args.paths and path not in args.paths:
                    continue
                for api in ("aio", "sync"):
                    for profile in args.profiles:
                        count, concurrency, timeout = {
                            "fast": (512, 128, 3),
                            "slow": (128, 32, 0.05),
                            "burst": (1000, 128, 3),
                        }[profile]
                        for repeat in range(args.repetitions):
                            name = f"{path}-{api}-{profile}-{repeat}"
                            command = base + [
                                "--address",
                                address,
                                "--api",
                                api,
                                "--profile",
                                profile,
                                "--requests",
                                str(count),
                                "--concurrency",
                                str(concurrency),
                                "--timeout",
                                str(timeout),
                            ]
                            client_env = None
                            if path.startswith("windows") and args.client_grpc_path:
                                client_env = dict(
                                    os.environ,
                                    PYTHONPATH=os.pathsep.join(
                                        [str((ROOT / args.client_grpc_path).resolve()), str(ROOT / "src")]
                                    ),
                                )
                            data = json.loads(run(command, env=client_env))
                            file = OUT / f"{name}.json"
                            file.write_text(json.dumps(data, indent=2), encoding="utf-8")
                            summary = {k: v for k, v in data.items() if k != "rows"}
                            summary.update(case=name, raw_file=str(file.relative_to(ROOT)))
                            report["cases"].append(summary)
                            (OUT / "matrix.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                            print(
                                f"{name}: {data['codes']}, lateOK={data['late_ok']}, p99={data['p99_all_ms']:.2f}ms",
                                flush=True,
                            )

        cases(paths)
        if args.paths and set(args.paths) == {"windows-native-direct"}:
            report["completed"] = True
            return
        run(compose + ["up", "--build", "-d"], timeout=600)
        ready("127.0.0.1:50251")
        cases(
            [
                ("windows-wsl-direct", [sys.executable, "scripts/deadline_probe.py"], "127.0.0.1:50251"),
                ("windows-wsl-envoy", [sys.executable, "scripts/deadline_probe.py"], "127.0.0.1:50250"),
                (
                    "linux-direct",
                    compose + ["exec", "-T", "probe", "python", "/app/scripts/deadline_probe.py"],
                    "probe:50051",
                ),
                (
                    "linux-envoy",
                    compose + ["exec", "-T", "probe", "python", "/app/scripts/deadline_probe.py"],
                    "ingress:50050",
                ),
            ]
        )
        report["completed"] = True
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        if native:
            native.terminate()
            native.wait(timeout=10)
        try:
            if owns_project:
                run(compose + ["down", "--volumes", "--remove-orphans"])
            report["cleanup"] = "passed"
        finally:
            if keepalive:
                keepalive.stdin.close()
                keepalive.wait(timeout=10)
            report["finished_at_unix"] = time.time()
            (OUT / "matrix.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
