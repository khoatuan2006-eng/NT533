"""Canonical endpoint addresses shared by the control plane and data-plane callers."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

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
