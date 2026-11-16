"""Only configured proxy hops contribute to the authentication limiter address."""

import pytest
from starlette.requests import Request

from logos.client_address import get_client_ip


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("192.0.2.9", "203.0.113.5", "192.0.2.9"),
        ("172.16.0.2", "198.51.100.8, 203.0.113.5, 172.16.0.3", "203.0.113.5"),
        ("172.16.0.2", "203.0.113.5, 10.0.0.1", "10.0.0.1"),
        ("172.16.0.2", "172.16.0.3, 172.16.0.4", "172.16.0.3"),
        ("172.16.0.2", "2001:db8::1, 172.16.0.3", "2001:db8::1"),
        ("172.16.0.2", "203.0.113.5, invalid", "172.16.0.2"),
        ("172.16.0.2", "203.0.113.5, ", "172.16.0.2"),
        ("172.16.0.2", "", "172.16.0.2"),
    ],
)
def test_rightmost_untrusted_hop(monkeypatch, peer, forwarded, expected):
    monkeypatch.setenv("LOGOS_RATE_LIMIT_TRUSTED_PROXY_CIDRS", "172.16.0.0/12")
    request = Request({"type": "http", "client": (peer, 1234), "headers": [(b"x-forwarded-for", forwarded.encode())]})
    assert get_client_ip(request) == expected


def test_configured_ipv6_proxies(monkeypatch):
    monkeypatch.setenv("LOGOS_RATE_LIMIT_TRUSTED_PROXY_CIDRS", "fd00::/8, 172.16.0.0/12")
    request = Request(
        {"type": "http", "client": ("fd00::2", 1234), "headers": [(b"x-forwarded-for", b"2001:db8::1, fd00::3")]}
    )
    assert get_client_ip(request) == "2001:db8::1"


def test_no_peer():
    assert get_client_ip(Request({"type": "http", "headers": []})) == "unknown"
