import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from logos.billing import provider_spend
from logos.billing.finalize import finalize_billing_inputs
from logos.billing.provider_spend import _call as real_call


def response(status, json=None):
    return httpx.Response(status, json=json, request=httpx.Request("POST", "http://ledger.test"))


@pytest.mark.parametrize("status", [402, 422, 503, 401])
async def test_reserve_fails_closed(monkeypatch, status):
    async def call(operation, body):
        assert operation == "reserve"
        assert body["provider_id"] == 22
        return response(status)

    monkeypatch.setattr(provider_spend, "_call", call)
    with pytest.raises(HTTPException) as exc:
        await provider_spend.reserve(22)
    assert exc.value.status_code == (status if status in {402, 422} else 503)


async def test_reserve_sends_only_request_shape(monkeypatch):
    attempt = uuid.uuid4()
    url = "https://api.anthropic.com/v1/messages"

    async def call(operation, body):
        assert operation == "reserve"
        assert body == {
            "provider_id": 22,
            "model_id": 33,
            "upstream_url": url,
            "shape": {
                "model": "synthetic-20261001",
                "messages": [{"role": "", "content": [{"type": "text", "text": ""}]}],
                "system": "",
                "user": "",
                "max_tokens": 400,
                "unsupported": None,
            },
        }
        return response(200, {"attempt_id": str(attempt)})

    monkeypatch.setattr(provider_spend, "_call", call)
    payload = {
        "model": "synthetic-20261001",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "SENSITIVE"}]}],
        "system": "SENSITIVE",
        "user": "SENSITIVE",
        "max_tokens": 400,
        "SENSITIVE FIELD": {"private": "SENSITIVE"},
    }
    assert await provider_spend.reserve(22, 33, url=url, payload=payload) == attempt


@pytest.mark.parametrize("invalid", [{}, {"attempt_id": "invalid"}, [], None])
async def test_unconfirmed_hold_is_not_admitted(monkeypatch, invalid):
    async def call(operation, body):
        return response(200, invalid)

    monkeypatch.setattr(provider_spend, "_call", call)
    with pytest.raises(HTTPException, match="503"):
        await provider_spend.reserve(22)


async def test_release_requires_no_dispatch_and_settle_requires_real_report(monkeypatch):
    attempt = uuid.uuid4()
    seen = []

    async def call(operation, body):
        seen.append((operation, body))
        return response(200)

    monkeypatch.setattr(provider_spend, "_call", call)
    reported = {"prompt_tokens": 5, "completion_tokens": 3}
    await provider_spend.settle(None, reported, "flex")
    await provider_spend.release_undispatched(None)
    for unknown in (
        {**reported, "usage_estimated": 1},
        {"prompt_tokens": 5},
        {"prompt_tokens": True, "completion_tokens": 3},
        {"prompt_tokens": -5, "completion_tokens": 3},
    ):
        await provider_spend.settle(attempt, unknown, "flex")
    assert seen == []
    await provider_spend.settle(attempt, reported, "flex")
    await provider_spend.release_undispatched(attempt)
    assert seen == [
        ("settle", {"attempt_id": str(attempt), "usage": reported, "tier": "flex", "undispatched": False}),
        ("settle", {"attempt_id": str(attempt), "undispatched": True}),
    ]


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
async def test_endpoint_configuration_fails_closed_before_opening_a_connection(monkeypatch, base, secret):
    monkeypatch.setenv("LOGOS_WEBSERVICE_URL", base)
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", secret)
    monkeypatch.setattr(provider_spend, "_call", real_call)
    with pytest.raises(HTTPException) as exc:
        await provider_spend.reserve(22)
    assert exc.value.status_code == 503


async def test_internal_wire_uses_only_configured_client_and_routes(monkeypatch):
    seen = []
    attempt = uuid.uuid4()

    def handler(request):
        seen.append((request.url.path, request.headers.get("Authorization"), request.content))
        if request.url.path.endswith("/reserve"):
            return httpx.Response(200, json={"attempt_id": str(attempt)})
        return httpx.Response(200)

    real_client = httpx.AsyncClient

    def client(**settings):
        assert settings == {"timeout": 5, "follow_redirects": False, "trust_env": False}
        return real_client(transport=httpx.MockTransport(handler), **settings)

    monkeypatch.setenv("LOGOS_WEBSERVICE_URL", "http://ledger.test:8081/")
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", "local-test-secret")
    monkeypatch.setattr(provider_spend, "_call", real_call)
    monkeypatch.setattr(provider_spend.httpx, "AsyncClient", client)
    assert (
        await provider_spend.reserve(
            22,
            3,
            url="https://api.openai.com/v1/responses",
            payload={"model": "fixture-revision", "input": "private prompt", "max_output_tokens": 10},
        )
        == attempt
    )
    await provider_spend.settle(attempt, {"prompt_tokens": 5, "completion_tokens": 3}, None)
    await provider_spend.release_undispatched(attempt)

    assert [path for path, _, _ in seen] == [
        "/internal/provider-spend/reserve",
        "/internal/provider-spend/settle",
        "/internal/provider-spend/settle",
    ]
    assert {auth for _, auth, _ in seen} == {"Bearer local-test-secret"}
    assert b"private prompt" not in seen[0][2]


async def test_settlement_connection_failure_does_not_release_or_retry(monkeypatch, caplog):
    checked = []

    async def call(operation, body):
        checked.append((operation, body))
        raise httpx.ConnectError("synthetic connection failure")

    monkeypatch.setattr(provider_spend, "_call", call)
    await provider_spend.settle(uuid.uuid4(), {"prompt_tokens": 8, "completion_tokens": 1}, "flex")
    assert [operation for operation, _ in checked] == ["settle"]
    assert checked[0][1]["undispatched"] is False
    assert "keeping reservation" in caplog.text


def test_request_shape_fences_nested_or_oversized_unknown_metadata():
    nested = {"text": "not carried"}
    for _ in range(9):
        nested = {"stream_options": nested}
    response = provider_spend._shape(
        {
            "model": "x" * 201,
            "messages": [{"role": "user", "content": [{"type": "image", "text": "not carried"}]}],
            "max_tokens": 2**31,
            "n": True,
            "stream_options": nested,
        }
    )
    assert response["model"] == ""
    assert response["max_tokens"] is None
    assert response["n"] is None
    assert response["messages"] == [{"role": "", "content": [{"type": "", "text": ""}]}]
    assert "unsupported" in str(response["stream_options"])
    assert "not carried" not in str(response)


def test_gateway_settlement_fixture_uses_real_orchestrator_finalized_usage():
    fixture = (
        Path(__file__).resolve().parents[4]
        / "logos-webservice/src/test/resources/fixtures/provider-spend-orchestrator-usage.json"
    )
    data = json.loads(fixture.read_text(encoding="utf-8"))
    usage, tier = finalize_billing_inputs(data["request"], data["response"], data["path"])
    assert usage == data["usage"]
    assert tier is None
