"""Validate deployment settings and resolve discovery endpoints before publication."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

REGISTRY_LEASE = 15.0
FETCH_TIMEOUT = 4.0
REFRESH_INTERVAL = 1.0
Membership = dict[str, list[str]]


def split_address(value: str, *, allow_empty_host: bool = False) -> tuple[str, int]:
    if not isinstance(value, str) or value.strip() != value:
        raise ValueError("address must be a host:port string")
    host, separator, port = value.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        ipaddress.IPv6Address(host)
    elif ":" in host:
        raise ValueError("IPv6 addresses require brackets")
    if not separator or not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("address port must be 1..65535")
    if not host and not allow_empty_host:
        raise ValueError("endpoint host is required")
    return host, int(port)


def join_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_config(data: str | bytes) -> Membership:
    document = json.loads(data, object_pairs_hook=_unique_object)
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "services"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or not isinstance(document["services"], dict)
        or not document["services"]
    ):
        raise ValueError("schema_version=1 and nonempty services required")
    for name, addresses in document["services"].items():
        if not re.fullmatch(r"[a-z0-9-]{1,128}", name) or not isinstance(addresses, list):
            raise ValueError(f"invalid service definition: {name}")
        for address in addresses:
            split_address(address)
    return document["services"]


class EndpointResolver:
    """Health probes and EDS share the same canonical IP:port membership."""

    async def resolve(self, members: Membership) -> Membership:
        resolved = {}
        for name, addresses in sorted(members.items()):
            endpoints = set()
            for address in sorted(addresses):
                host, port = split_address(address)
                try:
                    ips = [str(ipaddress.ip_address(host))]
                except ValueError:
                    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                    ips = sorted({row[4][0] for row in rows})
                if not ips:
                    raise ValueError(f"no IP for {host}")
                for ip in ips:
                    if ipaddress.ip_address(ip).is_unspecified:
                        raise ValueError("wildcard endpoint cannot be advertised")
                    endpoint = join_address(ip, port)
                    if endpoint in endpoints:
                        raise ValueError(f"duplicate endpoint: {endpoint}")
                    endpoints.add(endpoint)
            resolved[name] = sorted(endpoints)
        return resolved


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    discovery_path: str = "/config/discovery.json"
    listen_address: str = "[::]:18000"
    consul_address: str = ""
    active_health: bool = False

    def __post_init__(self):
        if not self.discovery_path or self.discovery_path.strip() != self.discovery_path:
            raise ValueError("DISCOVERY_CONFIG must be a nonempty path without surrounding whitespace")
        split_address(self.listen_address, allow_empty_host=True)
        if self.consul_address:
            url = urlsplit(self.consul_address)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or (url.port is not None and not 1 <= url.port <= 65535)
            ):
                raise ValueError("CONSUL_HTTP_ADDR must be an HTTP(S) agent URL")
        if self.active_health and not self.consul_address:
            raise ValueError("active registry health requires CONSUL_HTTP_ADDR")

    @classmethod
    def from_environment(cls, values: Mapping[str, str] | None = None) -> RuntimeSettings:
        values = os.environ if values is None else values
        active = values.get("CONSUL_ACTIVE_HEALTH", "")
        if active not in {"", "0", "false", "1", "true"}:
            raise ValueError("CONSUL_ACTIVE_HEALTH must be 0/1 or false/true")
        return cls(
            values.get("DISCOVERY_CONFIG") or "/config/discovery.json",
            values.get("XDS_LISTEN_ADDRESS") or "[::]:18000",
            values.get("CONSUL_HTTP_ADDR", ""),
            active in {"1", "true"},
        )
