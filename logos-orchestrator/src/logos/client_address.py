"""Resolve forwarded addresses only across configured proxy hops."""

import ipaddress
import os
from functools import lru_cache

from starlette.requests import HTTPConnection


@lru_cache(maxsize=16)
def _trusted_networks(cidrs: str) -> tuple:
    return tuple(ipaddress.ip_network(cidr.strip()) for cidr in cidrs.split(",") if cidr.strip())


def is_trusted_proxy(address: str) -> bool:
    """Whether a literal address belongs to a configured proxy network."""
    try:
        candidate = ipaddress.ip_address(address)
        networks = _trusted_networks(os.getenv("LOGOS_RATE_LIMIT_TRUSTED_PROXY_CIDRS", "172.16.0.0/12"))
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
