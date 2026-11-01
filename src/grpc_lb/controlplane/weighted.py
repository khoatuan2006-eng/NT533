"""Experimental xDS locality weighting; no policy is enabled by default."""

from envoy.config.core.v3 import base_pb2
from envoy.config.endpoint.v3 import endpoint_pb2
from google.protobuf.wrappers_pb2 import UInt32Value

from .resources import EDS, Snapshot, packed


def weighted_snapshot(snapshot: Snapshot, service: str, weights: dict[str, int]) -> Snapshot:
    """Rebuild EDS with one endpoint per locality, preserving only discovered endpoints."""
    import hashlib

    assignment = endpoint_pb2.ClusterLoadAssignment()
    if service not in snapshot.resources[EDS] or not snapshot.resources[EDS][service].Unpack(assignment):
        raise ValueError("unknown service")
    endpoints = {}
    for locality in assignment.endpoints:
        for endpoint in locality.lb_endpoints:
            address = endpoint.endpoint.address.socket_address
            host = f"[{address.address}]" if ":" in address.address else address.address
            endpoints[f"{host}:{address.port_value}"] = endpoint
    if set(weights) != set(endpoints) or not weights:
        raise ValueError("weights must exactly match nonempty current membership")
    if any(type(value) is not int or not 1 <= value <= 100 for value in weights.values()):
        raise ValueError("weights must be integers in 1..100")
    del assignment.endpoints[:]
    for index, address in enumerate(sorted(endpoints)):
        assignment.endpoints.add(
            locality=base_pb2.Locality(region="local", sub_zone=f"endpoint-{index}"),
            load_balancing_weight=UInt32Value(value=weights[address]),
            lb_endpoints=[endpoints[address]],
        )
    resources = {kind: dict(entries) for kind, entries in snapshot.resources.items()}
    resources[EDS][service] = packed(assignment)
    digest = hashlib.sha256()
    for kind, entries in resources.items():
        digest.update(kind.encode())
        for name, resource in sorted(entries.items()):
            digest.update(name.encode())
            digest.update(resource.SerializeToString(deterministic=True))
    return Snapshot(digest.hexdigest(), resources)
