"""CLI entry point and compatibility imports for the distributed workload service.

Implementation lives in processing, dependencies, service_runtime and application.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .application import ServiceApplication, ServiceSettings
from .dependencies import DependencyPool, NoBackend, Pool, XdsDependencyPool
from .processing import Service
from .service_identity import RPC_SERVICE, health_name
from .service_runtime import RunningService, start_service

__all__ = [
    "DependencyPool",
    "NoBackend",
    "Pool",
    "XdsDependencyPool",
    "Service",
    "RPC_SERVICE",
    "health_name",
    "RunningService",
    "start_service",
    "serve",
]


async def serve(settings: ServiceSettings | None = None) -> None:
    await ServiceApplication(settings or ServiceSettings.from_environment()).run()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-config", action="store_true", help="Validate deployment settings without starting a server"
    )
    args = parser.parse_args()
    try:
        settings = ServiceSettings.from_environment()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.check_config:
        print(
            json.dumps(
                {
                    "service_id": settings.service_id,
                    "instance_id": settings.instance_id,
                    "discovery_mode": settings.discovery_mode,
                    "listen_address": settings.listen_address,
                    "advertise_address": settings.advertise_address,
                },
                indent=2,
            )
        )
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
