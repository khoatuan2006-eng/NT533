"""Repeat fault scenarios against a running lab and report the spread across repetitions.

Each repetition sends a fixed-rate RPC trace through the ingress, injects one fault,
holds it, restores it, and keeps the raw trace. Start the lab first:

    python scripts/labenv.py up --variant xds --build
    python scripts/run_faults.py --repetitions 5
    python scripts/run_faults.py --scenarios partition-node3 quorum-loss --repetitions 3
    python scripts/run_faults.py --report results/lab-faults/<run>      # rebuild tables only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import grpc
from controlled_probe import Probe, interruption, summarize
from labenv import NODES, VARIANTS, LabEnvironment, make_lab

from grpc_lb.generated import processing_pb2 as pb
from grpc_lb.generated import processing_pb2_grpc as rpc

ROOT = Path(__file__).resolve().parents[1]
INSTANCES = 6  # a-1, a-2, b-1, b-2, c-1, c-2
MAX_INTERRUPTION_S = 30  # project target for faults the system must ride through


@dataclass(frozen=True)
class Scenario:
    title: str
    inject: Callable[[LabEnvironment], object]
    restore: Callable[[LabEnvironment], object]
    # continue    = service must keep answering; interruption <= MAX_INTERRUPTION_S
    # fail-closed = xDS callers must refuse (UNAVAILABLE) rather than use stale endpoints
    # observe     = no pass/fail; the behaviour itself is the result
    expect: str
    # Minimum seconds to keep observing after restore; a long partition can need minutes to heal.
    min_after: float = 0


def _stop_leader(lab: LabEnvironment) -> str:
    leader = lab.consul_leader()
    lab.stop(leader)
    return leader


def _restart_all(lab: LabEnvironment) -> None:
    lab.heal()
    stopped = sorted({row["service"] for row in lab.containers() if not row["running"]})
    if stopped:
        lab.start(*stopped)


SCENARIOS: dict[str, Scenario] = {
    "backend-stop": Scenario(
        "Dừng một backend (b-2)", lambda lab: lab.stop("b-2"), lambda lab: lab.start("b-2"), "continue"
    ),
    "controlplane-stop": Scenario(
        "Dừng một control plane (controlplane-1)",
        lambda lab: lab.stop("controlplane-1"),
        lambda lab: lab.start("controlplane-1"),
        "continue",
    ),
    "consul-leader": Scenario("Dừng Consul leader", _stop_leader, _restart_all, "continue"),
    "quorum-loss": Scenario(
        "Mất quorum (dừng consul-2 và consul-3)",
        lambda lab: lab.stop("consul-2", "consul-3"),
        lambda lab: lab.start("consul-2", "consul-3"),
        "fail-closed",
    ),
    "node3-down": Scenario(
        "Node 3 chết (consul-3, b-2, c-2)",
        lambda lab: lab.stop_node("node3"),
        lambda lab: lab.start_node("node3"),
        "continue",
    ),
    "node1-down": Scenario(
        "Node 1 chết (envoy-1, controlplane-1, consul-1, a-1, b-1)",
        lambda lab: lab.stop_node("node1"),
        lambda lab: lab.start_node("node1"),
        "continue",
    ),
    "partition-node3": Scenario(
        "Cô lập node 3 khỏi node 1 và node 2",
        lambda lab: lab.partition(["node3"], ["node1", "node2"]),
        lambda lab: lab.heal(),
        "continue",
    ),
    "partition-node1": Scenario(
        "Cô lập node 1 (có Envoy) khỏi node 2 và node 3",
        lambda lab: lab.partition(["node1"], ["node2", "node3"]),
        lambda lab: lab.heal(),
        "observe",
        min_after=150,
    ),
    "delay-node3": Scenario(
        "Node 3 ở xa: thêm 100 ms độ trễ",
        lambda lab: lab.degrade("node3", delay_ms=100),
        lambda lab: lab.heal(),
        "continue",
    ),
    "loss-node3": Scenario(
        "Mạng kém: node 3 rớt 20% gói",
        lambda lab: lab.degrade("node3", loss_percent=20),
        lambda lab: lab.heal(),
        "observe",
    ),
}


async def wait_ready(address: str, timeout: float = 180) -> None:
    """Block until the whole call graph answers: 30 consecutive OKs covering every instance."""
    deadline = time.monotonic() + timeout
    seen: set[str] = set()
    streak = 0

    def identities(node):
        return [node.instance_id] + [v for child in node.children for v in identities(child)]

    async with grpc.aio.insecure_channel(address) as channel:
        stub = rpc.ProcessingServiceStub(channel)
        while time.monotonic() < deadline:
            try:
                result = await stub.Execute(
                    pb.TaskRequest(
                        request_id="lab-ready",
                        target_service="service-a",
                        workload="light",
                        remaining_hops=64,
                    ),
                    timeout=3,
                )
                streak += 1
                seen.update(identities(result))
            except grpc.aio.AioRpcError:
                streak = 0
                seen.clear()
            if streak >= 30 and len(seen) >= INSTANCES:
                return
            await asyncio.sleep(0.1)
    raise TimeoutError(f"lab not ready after {timeout:.0f}s; instances seen: {sorted(seen)}")


async def run_once(lab: LabEnvironment, name: str, mode: str, output: Path, args) -> dict:
    scenario = SCENARIOS[name]
    await wait_ready(lab.address)
    parameters = SimpleNamespace(
        address=lab.address,
        output=output,
        mode=mode,
        entry="service-a",
        workload="light",
        rate=args.rate,
        duration=args.before + args.hold + max(args.after, scenario.min_after),
        timeout=3,
        ready_timeout=30,
        max_inflight=256,
        warmup=40,
        fault_at=None,
        stable_window=5,
    )
    probe = Probe(parameters)
    events = {"fault": name, "mode": mode, "endpoint": lab.address, "expect": scenario.expect}
    task = asyncio.create_task(probe.run())
    injected = False
    try:
        while not hasattr(probe, "origin"):
            if task.done():
                await task
                raise RuntimeError("probe ended before measurement started")
            await asyncio.sleep(0.05)

        def clock():
            return time.perf_counter() - probe.origin

        await asyncio.sleep(max(0, args.before - clock()))
        parameters.fault_at = events["fault_dispatch_s"] = clock()
        injected = True
        detail = await asyncio.to_thread(scenario.inject, lab)
        if detail is not None:
            events["fault_detail"] = detail
        events["fault_confirmed_s"] = clock()
        await asyncio.sleep(max(0, args.before + args.hold - clock()))
        events["restore_dispatch_s"] = clock()
        await asyncio.to_thread(scenario.restore, lab)
        events["restore_confirmed_s"] = clock()
        await task
    finally:
        if injected and "restore_confirmed_s" not in events:
            # Never leave the lab broken for the next repetition.
            await asyncio.to_thread(_restart_all, lab)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        output.with_suffix(".events.json").write_text(json.dumps(events, indent=2), encoding="utf-8")
    return events


def analyse(trace: Path) -> dict:
    """Recompute one repetition from its raw trace and event marks."""
    events = json.loads(trace.with_suffix(".events.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in trace.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()]
    fault, restore = events["fault_dispatch_s"], events["restore_dispatch_s"]
    duration = max(row["scheduled_s"] for row in rows)
    phase = [row for row in rows if fault <= row["scheduled_s"] < restore]
    failures = [row for row in phase if row["code"] != "OK"]
    codes: dict[str, int] = {}
    for row in failures:
        codes[row["code"]] = codes.get(row["code"], 0) + 1
    served = sorted(
        {
            i
            for row in phase
            if row["code"] == "OK" and row["scheduled_s"] >= fault + 5
            for i in row["instances"]
        }
    )
    ok_latency = sorted(row["latency_ms"] for row in phase if row["code"] == "OK")
    after = summarize(rows, duration, restore, 5)
    return {
        "trace": trace.name,
        "fault": events["fault"],
        "mode": events["mode"],
        "expect": events["expect"],
        "scheduled_in_fault": len(phase),
        "errors_in_fault": len(failures),
        "error_percent_in_fault": len(failures) / len(phase) * 100 if phase else None,
        "error_codes_in_fault": codes,
        "interruption_s": interruption(rows, fault, restore)["rpc_interruption_s"],
        "p95_ok_in_fault_ms": ok_latency[int((len(ok_latency) - 1) * 0.95)] if ok_latency else None,
        "instances_serving_in_fault": served,
        "recovery_after_restore_s": after["observed_recovery_s"],
    }


def verdict(runs: list[dict]) -> str:
    expect, mode = runs[0]["expect"], runs[0]["mode"]
    if expect == "observe" or (expect == "fail-closed" and VARIANTS.get(mode, ("xds",))[0] != "xds"):
        return "quan sát"
    if any(run["recovery_after_restore_s"] is None for run in runs):
        return "KHÔNG ĐẠT: không phục hồi sau khi khôi phục"
    if expect == "continue":
        worst = [run["interruption_s"] for run in runs]
        if any(value is None for value in worst):
            return "KHÔNG ĐẠT: chưa hội tụ trong lúc lỗi"
        return "đạt" if max(worst) <= MAX_INTERRUPTION_S else f"KHÔNG ĐẠT: gián đoạn > {MAX_INTERRUPTION_S}s"
    refused = all(run["error_codes_in_fault"].get("UNAVAILABLE", 0) > 0 for run in runs)
    return "đạt" if refused else "KHÔNG ĐẠT: vẫn phục vụ bằng endpoint cũ"


def report(directory: Path) -> str:
    runs, incomplete = [], []
    for path in sorted(directory.glob("*.events.json")):
        trace = path.with_name(path.name.removesuffix(".events.json"))
        try:
            runs.append(analyse(trace))
        except (KeyError, OSError, ValueError):
            # A repetition that crashed before restoring has no usable timeline.
            incomplete.append(trace.name)
    groups: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        groups.setdefault((run["fault"], run["mode"]), []).append(run)

    def spread(values, digits=2):
        values = [value for value in values if value is not None]
        if not values:
            return "—"
        return f"{statistics.median(values):.{digits}f} [{min(values):.{digits}f}–{max(values):.{digits}f}]"

    lines = [
        "# Kết quả thử lỗi trên lab",
        "",
        "Mỗi ô số là median [min–max] giữa các lần lặp. Gián đoạn tính từ RPC lỗi đầu tiên tới đầu cửa sổ "
        "5 giây toàn OK, trong lúc lỗi còn hiệu lực; 0 nghĩa là không ghi nhận lỗi ở tần suất lấy mẫu.",
        "",
        "| Tình huống | Mode | Lần lặp | Gián đoạn (s) | Lỗi trong lúc lỗi (%) | P95 OK (ms) "
        "| Phục hồi sau khôi phục (s) | Instance còn phục vụ | Kết luận |",
        "|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    summary = []
    for (fault, mode), group in groups.items():
        unconverged = sum(run["interruption_s"] is None for run in group)
        serving = sorted(set().union(*(run["instances_serving_in_fault"] for run in group)))
        result = verdict(group)
        title = SCENARIOS[fault].title if fault in SCENARIOS else fault
        lines.append(
            f"| {title} | {mode} | {len(group)} | "
            f"{spread([run['interruption_s'] for run in group])}"
            + (f" ({unconverged} lần chưa hội tụ)" if unconverged else "")
            + f" | {spread([run['error_percent_in_fault'] for run in group])} | "
            f"{spread([run['p95_ok_in_fault_ms'] for run in group], 1)} | "
            f"{spread([run['recovery_after_restore_s'] for run in group])} | "
            f"{', '.join(serving) or '—'} | {result} |"
        )
        summary.append({"fault": fault, "mode": mode, "verdict": result, "runs": group})
    lines += [
        "",
        f"Tiêu chí: tình huống “tiếp tục phục vụ” đạt khi gián đoạn ≤ {MAX_INTERRUPTION_S} giây ở mọi lần lặp; "
        "tình huống mất quorum đạt khi caller xDS trả UNAVAILABLE thay vì dùng endpoint cũ.",
        "“Instance còn phục vụ” lấy từ các RPC thành công sau 5 giây đầu của lỗi.",
        "Lab chạy trên một máy: các node chung CPU, không có độ trễ mạng thật; không dùng số liệu này để "
        "kết luận về hiệu năng.",
        "",
    ]
    if incomplete:
        lines += [f"Lần lặp không hoàn tất, không tính vào bảng: {', '.join(incomplete)}.", ""]
    text = "\n".join(lines)
    (directory / "report.md").write_text(text, encoding="utf-8")
    (directory / "report.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return text


async def suite(args, lab: LabEnvironment, directory: Path) -> None:
    for mode in args.variants:
        await asyncio.to_thread(lab.up_variant, mode)
        for name in args.scenarios:
            for repetition in range(1, args.repetitions + 1):
                trace = directory / f"{name}-{mode}-r{repetition}.json"
                print(f"[{mode}] {name} lần {repetition}/{args.repetitions}", flush=True)
                await run_once(lab, name, mode, trace, args)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    parser.add_argument("--variants", nargs="+", choices=list(VARIANTS), default=["xds"])
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument(
        "--before", type=float, default=10, help="Seconds of healthy traffic before the fault"
    )
    parser.add_argument("--hold", type=float, default=40, help="Seconds the fault stays active")
    parser.add_argument("--after", type=float, default=60, help="Seconds observed after restoring")
    parser.add_argument("--output", type=Path, help="Run directory (default: results/lab-faults/<timestamp>)")
    parser.add_argument("--report", type=Path, help="Only rebuild report.md from an existing run directory")
    parser.add_argument("--wsl", help="WSL distribution holding Docker (Windows only)")
    parser.add_argument("--compose", help="Compose binary on the Docker host")
    parser.add_argument("--env", help="Lab description (JSON) for a multi-VM lab; default is this machine")
    args = parser.parse_args()
    if args.report:
        print(report(args.report))
        return
    if args.repetitions < 1 or args.rate <= 0 or args.before < 5 or args.hold < 20 or args.after < 10:
        parser.error("repetitions >= 1, rate > 0, before >= 5, hold >= 20 and after >= 10 required")
    directory = args.output or ROOT / "results/lab-faults" / time.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=False)
    lab = make_lab(wsl=args.wsl, compose=args.compose, env=args.env)
    (directory / "run.json").write_text(
        json.dumps(
            {
                "started_unix_s": time.time(),
                "provenance": lab.provenance(),
                "nodes": NODES,
                "parameters": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        with lab.keepalive(), lab.lock(f"run_faults -> {directory.name}"):
            asyncio.run(suite(args, lab, directory))
    finally:
        # Report whatever finished, even if a later repetition failed.
        if any(directory.glob("*.events.json")):
            print(report(directory))


if __name__ == "__main__":
    main()
