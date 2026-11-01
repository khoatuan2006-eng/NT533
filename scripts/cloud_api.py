"""Operator-only OpenStack API session. Credentials stay outside release archives.

Requires requests and PyYAML (also provided by python-openstackclient).
Never log authentication bodies, headers or tokens.
"""

import argparse
import json
import time
from pathlib import Path

import requests
import yaml
from urllib3.util.retry import Retry


class Cloud:
    def __init__(self, path: Path, name="openstack"):
        config = yaml.safe_load(path.read_text())["clouds"][name]
        auth = config["auth"]
        url = auth["auth_url"].rstrip("/")
        self.identity = url if url.endswith("/v3") else url + "/v3"
        response = requests.post(
            self.identity + "/auth/tokens",
            json={
                "auth": {
                    "identity": {
                        "methods": ["application_credential"],
                        "application_credential": {
                            "id": auth["application_credential_id"],
                            "secret": auth["application_credential_secret"],
                        },
                    }
                }
            },
            timeout=30,
        )
        response.raise_for_status()
        self.user_id = response.json()["token"]["user"]["id"]
        self.application_credential_id = auth["application_credential_id"]
        self.session = requests.Session()
        self.session.mount(
            "https://",
            requests.adapters.HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    backoff_factor=0.5,
                    allowed_methods=frozenset({"GET", "HEAD", "PUT"}),
                    status_forcelist=(429, 502, 503, 504),
                )
            ),
        )
        self.session.headers["X-Auth-Token"] = response.headers["X-Subject-Token"]
        self.endpoints = {
            s["type"]: next(
                e["url"].rstrip("/")
                for e in s["endpoints"]
                if e["interface"] == "public" and e.get("region") == config.get("region_name", "RegionOne")
            )
            for s in response.json()["token"]["catalog"]
        }

    def request(self, service, method, path, body=None):
        response = self.session.request(method, self.endpoints[service] + path, json=body, timeout=30)
        if not response.ok:
            # Resource errors are useful; never include request headers/body.
            raise RuntimeError(
                f"{service} {method} {path}: HTTP {response.status_code}: {response.text[:1500]}"
            )
        return response.json() if response.content else None

    def wait_lb(self, identity):
        for _ in range(90):
            lb = self.request("load-balancer", "GET", f"/v2/lbaas/loadbalancers/{identity}")["loadbalancer"]
            if lb["provisioning_status"] == "ACTIVE":
                return lb
            if lb["provisioning_status"] == "ERROR":
                raise RuntimeError("Load balancer entered ERROR")
            time.sleep(2)
        raise TimeoutError("Load balancer provisioning timed out")

    def revoke_own_credential(self):
        response = self.session.delete(
            f"{self.identity}/users/{self.user_id}/application_credentials/{self.application_credential_id}",
            timeout=30,
        )
        response.raise_for_status()


def configure(cloud, lb_id, members):
    prefix = "/v2/lbaas"
    service = "load-balancer"
    lb = cloud.wait_lb(lb_id)
    if lb["provider"] != "ovn":
        raise ValueError("This configuration is specifically for provider OVN")
    listeners = cloud.request(service, "GET", f"{prefix}/listeners?loadbalancer_id={lb_id}")["listeners"]
    if listeners:
        listener = next(x for x in listeners if x["name"] == "lb-ha-grpc-tcp")
        if (listener["protocol"], listener["protocol_port"]) != ("TCP", 80):
            raise ValueError("Existing listener differs from desired TCP:80")
    else:
        listener = cloud.request(
            service,
            "POST",
            f"{prefix}/listeners",
            {
                "listener": {
                    "name": "lb-ha-grpc-tcp",
                    "protocol": "TCP",
                    "protocol_port": 80,
                    "loadbalancer_id": lb_id,
                }
            },
        )["listener"]
        cloud.wait_lb(lb_id)
    pools = cloud.request(service, "GET", f"{prefix}/pools?loadbalancer_id={lb_id}")["pools"]
    if pools:
        pool = next(x for x in pools if x["name"] == "lb-ha-envoy-pool")
        if pool["lb_algorithm"] != "SOURCE_IP_PORT":
            raise ValueError("Existing pool uses a different algorithm")
    else:
        pool = cloud.request(
            service,
            "POST",
            f"{prefix}/pools",
            {
                "pool": {
                    "name": "lb-ha-envoy-pool",
                    "protocol": "TCP",
                    "lb_algorithm": "SOURCE_IP_PORT",
                    "listener_id": listener["id"],
                }
            },
        )["pool"]
        cloud.wait_lb(lb_id)
    current = cloud.request(service, "GET", f"{prefix}/pools/{pool['id']}/members")["members"]
    desired = {(host, 18080) for host in members}
    if any((m["address"], m["protocol_port"]) not in desired for m in current):
        raise ValueError("Unexpected pool member; refusing to silently alter it")
    for host in members:
        if any(m["address"] == host and m["protocol_port"] == 18080 for m in current):
            continue
        cloud.request(
            service,
            "POST",
            f"{prefix}/pools/{pool['id']}/members",
            {
                "member": {
                    "address": host,
                    "protocol_port": 18080,
                    "subnet_id": lb["vip_subnet_id"],
                    "weight": 1,
                }
            },
        )
        cloud.wait_lb(lb_id)
    pool = cloud.request(service, "GET", f"{prefix}/pools/{pool['id']}")["pool"]
    if not pool.get("healthmonitor_id"):
        cloud.request(
            service,
            "POST",
            f"{prefix}/healthmonitors",
            {
                "healthmonitor": {
                    "name": "lb-ha-tcp-health",
                    "pool_id": pool["id"],
                    "type": "TCP",
                    "delay": 2,
                    "timeout": 1,
                    "max_retries": 2,
                    "max_retries_down": 2,
                }
            },
        )
        cloud.wait_lb(lb_id)
    return cloud.request(service, "GET", f"{prefix}/loadbalancers/{lb_id}/status")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--loadbalancer", required=True)
    parser.add_argument("--members", nargs=2, required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = configure(Cloud(args.credentials), args.loadbalancer, args.members)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("HA resources configured; verify traffic and faults separately.")


if __name__ == "__main__":
    main()
