import asyncio

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from logos.billing import provider_spend


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setenv("LOGOS_WEBSERVICE_URL", "http://ledger.test:8081/")
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", "local-test-secret")


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("provider_id", [None, -1, 0, 2**31, True, "22"])
async def test_check_rejects_invalid_provider_id(provider_id):
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(provider_id)


async def test_unknown_and_expired_snapshot_fail_closed(monkeypatch):
    monkeypatch.setattr(provider_spend, "_snapshot", None)
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(22)

    monkeypatch.setattr(provider_spend, "monotonic", lambda: 3)
    monkeypatch.setattr(provider_spend, "_snapshot", provider_spend.SpendSnapshot(frozenset({22}), 3))
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(22)


async def test_warm_checks_do_not_fetch_and_allow_uncapped_providers(monkeypatch, endpoint):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("Authorization"), request.content))
        return httpx.Response(200, json={"blocked_provider_ids": [22], "valid_for_ms": 5_000})

    async with client_for(handler) as client:
        await provider_spend._refresh(client)
        for _ in range(100):
            await provider_spend.check(23)
            with pytest.raises(HTTPException, match="402"):
                await provider_spend.check(22)

    assert seen == [("GET", "/internal/provider-spend/status", "Bearer local-test-secret", b"")]


async def test_slow_response_does_not_extend_snapshot_deadline(monkeypatch, endpoint):
    clock = [10.0]
    monkeypatch.setattr(provider_spend, "monotonic", lambda: clock[0])

    def handler(request):
        clock[0] = 11
        return httpx.Response(200, json={"blocked_provider_ids": [], "valid_for_ms": 2_000})

    async with client_for(handler) as client:
        await provider_spend._refresh(client)
    clock[0] = 11.999
    await provider_spend.check(22)
    clock[0] = 12
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(22)


async def test_zero_lease_fails_closed(monkeypatch, endpoint):
    monkeypatch.setattr(provider_spend, "monotonic", lambda: 10)
    monkeypatch.setattr(provider_spend, "_snapshot", None)
    async with client_for(
        lambda req: httpx.Response(200, json={"blocked_provider_ids": [], "valid_for_ms": 0})
    ) as client:
        with pytest.raises(ValueError, match="expired"):
            await provider_spend._refresh(client)
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(22)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"blocked_provider_ids": "22", "valid_for_ms": 1000},
        {"blocked_provider_ids": [True], "valid_for_ms": 1000},
        {"blocked_provider_ids": [0], "valid_for_ms": 1000},
        {"blocked_provider_ids": [2**31], "valid_for_ms": 1000},
        {"blocked_provider_ids": [], "valid_for_ms": True},
        {"blocked_provider_ids": [], "valid_for_ms": 5001},
        {"blocked_provider_ids": [], "valid_for_ms": -1},
    ],
)
async def test_malformed_refresh_retains_previous_lease_without_extension(monkeypatch, endpoint, payload):
    previous = provider_spend.SpendSnapshot(frozenset({22}), 30)
    monkeypatch.setattr(provider_spend, "_snapshot", previous)
    async with client_for(lambda req: httpx.Response(200, json=payload)) as client:
        with pytest.raises(ValueError):
            await provider_spend._refresh(client)
    assert provider_spend._snapshot is previous


@pytest.mark.parametrize("status", [301, 401, 404, 503])
async def test_failed_fetch_cannot_install_snapshot(monkeypatch, endpoint, status):
    monkeypatch.setattr(provider_spend, "_snapshot", None)
    async with client_for(lambda req: httpx.Response(status)) as client:
        with pytest.raises(httpx.HTTPError):
            await provider_spend._refresh(client)
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(22)


async def test_policy_only_refresh_allows_only_verified_uncapped_providers(monkeypatch):
    clock = [11]
    lookups = []
    previous = provider_spend.SpendSnapshot(frozenset({22}), 12)
    monkeypatch.setattr(provider_spend, "_snapshot", previous)
    monkeypatch.setattr(provider_spend, "monotonic", lambda: clock[0])

    def policies():
        lookups.append("lookup")
        return frozenset({22})

    monkeypatch.setattr(provider_spend, "_read_active_policy_ids", policies)
    await provider_spend._refresh_policies_if_needed()
    assert provider_spend._snapshot is previous
    assert lookups == []

    clock[0] = 12
    await provider_spend._refresh_policies_if_needed()
    for _ in range(100):
        await provider_spend.check(23)
        with pytest.raises(HTTPException, match="503"):
            await provider_spend.check(22)
    assert lookups == ["lookup"]
    clock[0] = 13
    await provider_spend._refresh_policies_if_needed()
    assert lookups == ["lookup", "lookup"]
    clock[0] = 17
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(23)


async def test_failed_policy_refresh_leaves_absent_state_closed(monkeypatch):
    monkeypatch.setattr(provider_spend, "_snapshot", None)

    def fail():
        raise SQLAlchemyError("connection unavailable")

    monkeypatch.setattr(provider_spend, "_read_active_policy_ids", fail)
    with pytest.raises(SQLAlchemyError):
        await provider_spend._refresh_policies_if_needed()
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.check(23)


@pytest.mark.parametrize(
    "base,secret",
    [
        ("", "test-secret"),
        ("ftp://ledger.test", "test-secret"),
        ("http:/no-host", "test-secret"),
        ("http://user@ledger.test", "test-secret"),
        ("http://ledger.test?settings=1", "test-secret"),
        ("http://ledger.test#local", "test-secret"),
        ("http://ledger.test", ""),
    ],
)
async def test_bad_endpoint_configuration_cannot_open_a_connection(monkeypatch, base, secret):
    monkeypatch.setenv("LOGOS_WEBSERVICE_URL", base)
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", secret)
    async with client_for(lambda req: pytest.fail("Invalid configuration must not begin a fetch")) as client:
        with pytest.raises(ValueError):
            await provider_spend._refresh(client)


async def test_lifespan_refresh_runs_in_background_and_cleans_up(monkeypatch):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def loop():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            stopped.set()

    def unavailable_db():
        raise SQLAlchemyError("connection unavailable")

    monkeypatch.setattr(provider_spend, "_read_active_policy_ids", unavailable_db)
    monkeypatch.setattr(provider_spend, "_refresh_loop", loop)
    async with provider_spend.refreshing():
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(HTTPException, match="503"):
            await provider_spend.check(22)
    assert stopped.is_set()
    assert provider_spend._snapshot is None


@pytest.mark.parametrize("webservice_failure", [404, httpx.ReadTimeout])
async def test_startup_prewarms_uncapped_providers_when_webservice_is_unavailable(
    monkeypatch, endpoint, webservice_failure
):
    lookups = []
    status_attempted = asyncio.Event()

    def policies():
        lookups.append("lookup")
        return frozenset({22})

    def handler(request):
        status_attempted.set()
        if webservice_failure == 404:
            return httpx.Response(404)
        raise webservice_failure("webservice is not ready")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        provider_spend.httpx,
        "AsyncClient",
        lambda **settings: real_client(transport=httpx.MockTransport(handler), **settings),
    )
    monkeypatch.setattr(provider_spend, "_read_active_policy_ids", policies)

    async with provider_spend.refreshing():
        # Serving starts with the policy-only state, not after a network request.
        assert lookups == ["lookup"]
        assert not status_attempted.is_set()
        await provider_spend.check(23)
        with pytest.raises(HTTPException, match="503"):
            await provider_spend.check(22)
        await asyncio.wait_for(status_attempted.wait(), timeout=1)
        await provider_spend.check(23)
        with pytest.raises(HTTPException, match="503"):
            await provider_spend.check(22)


async def test_startup_bounds_policy_wait_and_still_enters_lifespan(monkeypatch):
    cancelled = asyncio.Event()

    async def slow_db():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def background():
        await asyncio.Future()

    monkeypatch.setattr(provider_spend, "_try_refresh_policies", slow_db)
    monkeypatch.setattr(provider_spend, "_refresh_loop", background)
    monkeypatch.setattr(provider_spend, "_STARTUP_POLICY_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(1):
        async with provider_spend.refreshing():
            assert cancelled.is_set()
            with pytest.raises(HTTPException, match="503"):
                await provider_spend.check(22)
