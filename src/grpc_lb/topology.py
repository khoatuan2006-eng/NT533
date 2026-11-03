"""Validate a configurable service DAG before opening network resources."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    service_id: str
    instances: dict[str, str]
    dependencies: tuple[str, ...] = ()
    mode: str = "sequential"
    delay_s: float = 0.005
    concurrency: int = 50
    timeout_s: float = 5
    heavy_multiplier: float = 5
    failure_rate: float = 0


def split_endpoint(address: str) -> tuple[str, int]:
    """Only explicit host:port (or [IPv6]:port), never a URI or wildcard."""
    if not isinstance(address, str) or address != address.strip():
        raise ValueError("Endpoint must be a host:port string")
    host, separator, port = address.rpartition(":")
    if not separator or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"Invalid endpoint port: {address}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError(f"Invalid IPv6 endpoint: {address}") from exc
    elif not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?", host):
        raise ValueError(f"Invalid endpoint host: {address}")
    if host in ("0.0.0.0", "::"):
        raise ValueError("Advertised endpoint must not be a wildcard bind address")
    return host, int(port)


def _identifier(value) -> bool:
    return (
        isinstance(value, str) and bool(value) and value == value.strip() and all(ord(c) >= 32 for c in value)
    )


def _number(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate(services: dict[str, ServiceConfig]) -> list[str]:
    """Return dependencies-first startup order; reject cycles/ambiguous IDs."""
    if not isinstance(services, dict) or not services:
        raise ValueError("Topology must contain services")
    instance_ids, addresses = set(), set()
    for name, cfg in services.items():
        if not _identifier(name) or not isinstance(cfg, ServiceConfig) or name != cfg.service_id:
            raise ValueError("Each service needs a valid ID and ServiceConfig")
        if not isinstance(cfg.instances, dict) or not cfg.instances:
            raise ValueError("Each service needs an ID and instances")
        if cfg.mode not in ("sequential", "parallel"):
            raise ValueError(f"Invalid execution mode: {cfg.mode}")
        if type(cfg.concurrency) is not int or cfg.concurrency <= 0:
            raise ValueError("Concurrency must be a positive integer")
        for value, lower, inclusive in (
            (cfg.delay_s, 0, True),
            (cfg.timeout_s, 0, False),
            (cfg.heavy_multiplier, 1, True),
        ):
            if not _number(value) or (value < lower if inclusive else value <= lower):
                raise ValueError("Invalid delay, timeout or heavy multiplier")
        if not math.isfinite(cfg.delay_s * cfg.heavy_multiplier):
            raise ValueError("Heavy workload delay must be finite")
        if not _number(cfg.failure_rate) or not 0 <= cfg.failure_rate <= 1:
            raise ValueError("Failure rate must be between 0 and 1")
        if not isinstance(cfg.dependencies, (list, tuple)) or any(
            not _identifier(d) for d in cfg.dependencies
        ):
            raise ValueError("Dependencies must be a list of service IDs")
        if len(set(cfg.dependencies)) != len(cfg.dependencies) or any(
            d not in services for d in cfg.dependencies
        ):
            raise ValueError(f"Unknown or duplicate dependencies for {name}")
        for instance, address in cfg.instances.items():
            if not _identifier(instance) or instance in instance_ids:
                raise ValueError("Instance IDs and endpoints must be nonempty and globally unique")
            split_endpoint(address)
            if address in addresses:
                raise ValueError("Instance IDs and endpoints must be nonempty and globally unique")
            instance_ids.add(instance)
            addresses.add(address)
    order, visiting, done, depths = [], set(), set(), {}

    def visit(name: str) -> None:
        if name in visiting:
            raise ValueError("Topology contains a dependency cycle")
        if name in done:
            return
        if len(visiting) >= 64:
            raise ValueError("Topology exceeds the 64-hop execution limit")
        visiting.add(name)
        for dependency in services[name].dependencies:
            visit(dependency)
        depths[name] = 1 + max((depths[d] for d in services[name].dependencies), default=0)
        if depths[name] > 64:
            raise ValueError("Topology exceeds the 64-hop execution limit")
        visiting.remove(name)
        done.add(name)
        order.append(name)

    for name in services:
        visit(name)
    return order


def load_topology(path: str | Path) -> tuple[str, dict[str, ServiceConfig]]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "entry_service", "services"}:
        raise ValueError("Topology requires schema_version, entry_service and services")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported topology schema_version")
    if not isinstance(raw["services"], dict):
        raise ValueError("services must be an object")
    try:
        services = {
            name: ServiceConfig(service_id=name, **values) for name, values in raw["services"].items()
        }
    except TypeError as exc:
        raise ValueError(f"Invalid service configuration: {exc}") from exc
    validate(services)
    entry = raw["entry_service"]
    if not _identifier(entry) or entry not in services:
        raise ValueError("entry_service must exist")
    return entry, services
