"""Validate deployment settings and resolve discovery endpoints before publication."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..endpoints import EndpointResolver, Membership, join_address, split_address  # noqa: F401

REGISTRY_LEASE = 15.0
FETCH_TIMEOUT = 4.0
REFRESH_INTERVAL = 1.0


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


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    discovery_path: str = "/config/discovery.json"
    listen_address: str = "[::]:18000"
    consul_address: str = ""
    active_health: bool = False
    rl_telemetry: str = ""
    rl_service: str = ""
    rl_model: str = ""

    def __post_init__(self):
        if bool(self.rl_telemetry) != bool(self.rl_service):
            raise ValueError("EXPERIMENTAL_RL_TELEMETRY and EXPERIMENTAL_RL_SERVICE must be set together")
        if self.rl_model and not self.rl_service:
            raise ValueError("EXPERIMENTAL_RL_MODEL requires the RL telemetry settings")
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
            values.get("EXPERIMENTAL_RL_TELEMETRY", ""),
            values.get("EXPERIMENTAL_RL_SERVICE", ""),
            values.get("EXPERIMENTAL_RL_MODEL", ""),
        )
