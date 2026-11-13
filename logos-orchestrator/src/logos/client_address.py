"""Resolve forwarded addresses only across configured proxy hops."""

import ipaddress
import os
from functools import lru_cache

from starlette.requests import HTTPConnection

# The private address space: the hop in front of this service is a container
# of the stack, and Docker takes its address from the daemon's pool — 172.17
# to 172.31 while those are free, then 192.168.x. The service publishes no
# host port, so no other peer can reach it.
DEFAULT_TRUSTED_PROXY_CIDRS = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


@lru_cache(maxsize=16)
def _trusted_networks(cidrs: str) -> tuple:
    return tuple(ipaddress.ip_network(cidr.strip()) for cidr in cidrs.split(",") if cidr.strip())


def is_trusted_proxy(address: str) -> bool:
    """Whether a literal address belongs to a configured proxy network."""
    try:
        candidate = ipaddress.ip_address(address)
        networks = _trusted_networks(os.getenv("LOGOS_RATE_LIMIT_TRUSTED_PROXY_CIDRS", DEFAULT_TRUSTED_PROXY_CIDRS))
        return any(candidate in network for network in networks)
    except ValueError:
        return False


def get_client_ip(request: HTTPConnection) -> str:
    """Select the rightmost untrusted hop, starting with the socket peer."""
    peer = request.client.host if request.client else "unknown"
    address = peer
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return peer
    for hop in reversed(forwarded.split(",")):
        if not is_trusted_proxy(address):
            break
        try:
            address = str(ipaddress.ip_address(hop.strip()))
        except ValueError:
            # A malformed chain must not create arbitrary limiter buckets.
            return peer
    return address
