import uuid

import httpx
import pytest
from fastapi import HTTPException

from logos.billing import provider_spend


def response(status, json=None):
    return httpx.Response(status, json=json, request=httpx.Request("POST", "http://ledger.test"))


@pytest.mark.parametrize("status", [402, 422, 503, 401])
async def test_reserve_fails_closed(monkeypatch, status):
    async def call(operation, body):
        assert operation == "reserve"
        assert body["providerId"] == 22
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
            "providerId": 22,
            "modelId": 33,
            "upstreamUrl": url,
            "shape": {
                "model": "synthetic-20261001",
                "messages": [{"role": "", "content": [{"type": "text", "text": ""}]}],
                "system": "",
                "user": "",
                "max_tokens": 400,
                "unsupported": None,
            },
        }
        return response(200, {"attemptId": str(attempt)})

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


@pytest.mark.parametrize("invalid", [{}, {"attemptId": "invalid"}, [], None])
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
        ("settle", {"attemptId": str(attempt), "usage": reported, "tier": "flex", "undispatched": False}),
        ("settle", {"attemptId": str(attempt), "undispatched": True}),
    ]
