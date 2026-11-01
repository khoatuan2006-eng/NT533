"""Build standard Envoy LDS/CDS/EDS protobufs; no workload traffic passes here."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from envoy.config.cluster.v3 import cluster_pb2
from envoy.config.core.v3 import address_pb2, base_pb2, config_source_pb2, health_check_pb2
from envoy.config.endpoint.v3 import endpoint_components_pb2, endpoint_pb2
from envoy.config.listener.v3 import api_listener_pb2, listener_pb2
from envoy.config.route.v3 import route_components_pb2, route_pb2
from envoy.extensions.filters.http.router.v3 import router_pb2
from envoy.extensions.filters.network.http_connection_manager.v3 import http_connection_manager_pb2 as hcm_pb2
from google.protobuf.any_pb2 import Any
from google.protobuf.duration_pb2 import Duration
from google.protobuf.message import Message
from google.protobuf.wrappers_pb2 import UInt32Value

from .config import EndpointResolver, Membership, split_address

LDS = "type.googleapis.com/envoy.config.listener.v3.Listener"
CDS = "type.googleapis.com/envoy.config.cluster.v3.Cluster"
EDS = "type.googleapis.com/envoy.config.endpoint.v3.ClusterLoadAssignment"


def packed(message: Message) -> Any:
    result = Any()
    result.Pack(message, deterministic=True)
    return result


@dataclass(frozen=True, slots=True)
class Snapshot:
    version: str
    resources: dict[str, dict[str, Any]]


class ResourceBuilder:
    def __init__(self, resolver: EndpointResolver | None = None):
        self.resolver = resolver or EndpointResolver()

    async def build(self, members: Membership) -> Snapshot:
        members = await self.resolver.resolve(members)
        resources = {LDS: {}, CDS: {}, EDS: {}}
        ads = config_source_pb2.ConfigSource(
            resource_api_version=config_source_pb2.V3, ads=config_source_pb2.AggregatedConfigSource()
        )
        for name, addresses in members.items():
            endpoints = []
            for address in addresses:
                host, port = split_address(address)
                endpoints.append(
                    endpoint_components_pb2.LbEndpoint(
                        health_status=health_check_pb2.HEALTHY,
                        endpoint=endpoint_components_pb2.Endpoint(
                            address=address_pb2.Address(
                                socket_address=address_pb2.SocketAddress(address=host, port_value=port)
                            )
                        ),
                    )
                )
            assignment = endpoint_pb2.ClusterLoadAssignment(
                cluster_name=name,
                endpoints=[
                    endpoint_components_pb2.LocalityLbEndpoints(
                        locality=base_pb2.Locality(region="local"),
                        load_balancing_weight=UInt32Value(value=1),
                        lb_endpoints=endpoints,
                    )
                ],
            )
            cluster = cluster_pb2.Cluster(
                name=name,
                connect_timeout=Duration(seconds=1),
                type=cluster_pb2.Cluster.EDS,
                eds_cluster_config=cluster_pb2.Cluster.EdsClusterConfig(eds_config=ads, service_name=name),
                lb_policy=cluster_pb2.Cluster.ROUND_ROBIN,
            )
            routes = route_pb2.RouteConfiguration(
                name=name,
                virtual_hosts=[
                    route_components_pb2.VirtualHost(
                        name=name,
                        domains=["*"],
                        routes=[
                            route_components_pb2.Route(
                                match=route_components_pb2.RouteMatch(prefix="/"),
                                route=route_components_pb2.RouteAction(cluster=name),
                            )
                        ],
                    )
                ],
            )
            manager = hcm_pb2.HttpConnectionManager(
                stat_prefix=name,
                route_config=routes,
                http_filters=[
                    hcm_pb2.HttpFilter(
                        name="envoy.filters.http.router",
                        typed_config=packed(router_pb2.Router()),
                    )
                ],
            )
            listener = listener_pb2.Listener(
                name=name, api_listener=api_listener_pb2.ApiListener(api_listener=packed(manager))
            )
            resources[LDS][name] = packed(listener)
            resources[CDS][name] = packed(cluster)
            resources[EDS][name] = packed(assignment)
        digest = hashlib.sha256()
        for kind, entries in resources.items():
            digest.update(kind.encode())
            for name, resource in sorted(entries.items()):
                digest.update(name.encode())
                digest.update(resource.SerializeToString(deterministic=True))
        return Snapshot(digest.hexdigest(), resources)
