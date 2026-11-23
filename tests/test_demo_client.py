"""Demo client against an in-process fake A: lines, instance shares and error counting."""

import argparse
import asyncio
import sys
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo_client  # noqa: E402

from grpc_lb.generated import processing_pb2 as pb  # noqa: E402
from grpc_lb.generated import processing_pb2_grpc as rpc  # noqa: E402


class FakeA(rpc.ProcessingServiceServicer):
    """Answers like A: itself, then one B and one C, alternating b-1/b-2; every 4th call fails."""

    def __init__(self):
        self.calls = 0

    async def Execute(self, request, context):
        self.calls += 1
        if self.calls % 4 == 0:
            await context.abort(grpc.StatusCode.UNAVAILABLE, "down")
        b = "b-1" if self.calls % 2 else "b-2"
        child = [pb.TaskResult(instance_id=b), pb.TaskResult(instance_id="c-1")]
        return pb.TaskResult(request_id=request.request_id, instance_id="a-1", children=child)


async def exercise():
    server = grpc.aio.server()
    rpc.add_ProcessingServiceServicer_to_server(FakeA(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        args = argparse.Namespace(
            targets=[f"127.0.0.1:{port}"],
            name="t",
            rate=40,
            duration=0.5,
            timeout=2,
            summary_every=100,
            slow_ms=1000,
            quiet=False,
        )
        stats, lines = demo_client.Stats(), []
        await demo_client.run(args, stats, out=lines.append)
        return stats, lines
    finally:
        await server.stop(None)


def test_demo_client_reports_who_served_each_request_and_errors():
    stats, lines = asyncio.run(exercise())
    ok = [o for o in stats.outcomes if o.code == "OK"]
    failed = [o for o in stats.outcomes if o.code != "OK"]
    assert len(stats.outcomes) >= 15 and ok and failed
    assert all(o.instances[0] == "a-1" and o.instances[-1] == "c-1" for o in ok)
    assert all(o.code == "UNAVAILABLE" for o in failed)
    assert any("a-1 → b-1, c-1" in text for text in lines)
    summary = stats.summary()
    assert "tầng A: a-1 100%" in summary and "tầng C: c-1 100%" in summary
    assert "b-1" in summary and "b-2" in summary and "UNAVAILABLE" in summary


def test_percentile_and_empty_summary():
    assert demo_client.percentile([1, 2, 3, 4, 100], 50) == 3
    assert demo_client.Stats().summary() == "chưa có request"
