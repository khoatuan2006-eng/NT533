"""Run with python -m grpc_lb.controlplane or grpc-lb-controlplane."""

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .application import ControlPlane
from .config import EndpointResolver, RuntimeSettings, decode_config
from .rl_controller import RLController
from .sources import create_source


async def serve(settings: RuntimeSettings):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: loop.call_soon_threadsafe(stop.set))
        policy = (
            RLController(settings.rl_telemetry, settings.rl_service, model_path=settings.rl_model or None)
            if settings.rl_service
            else None
        )
        source = await create_source(settings)
        await ControlPlane(settings.listen_address, source, policy).run(stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-config", action="store_true", help="Validate settings and policy without binding"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = RuntimeSettings.from_environment()
    if args.check_config:
        members = decode_config(Path(settings.discovery_path).read_bytes())
        if not settings.consul_address:
            asyncio.run(EndpointResolver().resolve(members))
        print(f"configuration valid: services={','.join(sorted(members))} listen={settings.listen_address}")
        return
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
