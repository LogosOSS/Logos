from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import logos as main_mod
from logos.routers import admin as admin_mod
from logos.routers import internal as internal_mod


def _make_request(authorization: str = "") -> MagicMock:
    request = MagicMock()
    request.headers.get = lambda key, default="": authorization if key == "authorization" else default
    return request


@pytest.fixture(autouse=True)
def reset_pipeline(monkeypatch):
    monkeypatch.setattr(main_mod, "_pipeline", MagicMock(), raising=False)
    monkeypatch.setattr(main_mod, "_logosnode_facade", MagicMock(), raising=False)


@pytest.mark.asyncio
async def test_returns_403_when_secret_not_configured(monkeypatch):
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", None)
    with pytest.raises(HTTPException) as exc_info:
        await admin_mod.scheduler_state(_make_request("Bearer secret"))
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_returns_401_when_secret_is_wrong(monkeypatch):
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", "correct-secret")
    with pytest.raises(HTTPException) as exc_info:
        await admin_mod.scheduler_state(_make_request("Bearer wrong-secret"))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_returns_401_for_a_plain_api_key(monkeypatch):
    """A valid Logos API key is no longer accepted — only the internal secret is."""
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", "correct-secret")
    with pytest.raises(HTTPException) as exc_info:
        await admin_mod.scheduler_state(_make_request("Bearer lg-user-key"))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_returns_503_when_pipeline_not_initialized(monkeypatch):
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", "correct-secret")
    monkeypatch.setattr(main_mod, "_pipeline", None)
    response = await admin_mod.scheduler_state(_make_request("Bearer correct-secret"))
    assert response.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [None, "simple", "demand", "lru"])
@pytest.mark.parametrize("planner_enabled", [False, True])
async def test_returns_scheduler_payload_with_the_internal_secret(monkeypatch, policy, planner_enabled):
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", "correct-secret")
    pipeline = MagicMock()
    pipeline.scheduler.get_total_queue_depth.return_value = 7
    pipeline.scheduler._prefix_router = None
    if policy is None:
        del pipeline.scheduler._residency_policy
    else:
        pipeline.scheduler._residency_policy = policy
    facade = MagicMock()
    facade.debug_state.return_value = {"providers": {}}
    monkeypatch.setattr(main_mod, "_pipeline", pipeline)
    monkeypatch.setattr(main_mod, "_logosnode_facade", facade)

    planner = MagicMock(residency_policy=policy or "simple") if planner_enabled else None
    monkeypatch.setattr(main_mod, "_capacity_planner", planner)
    response = await admin_mod.scheduler_state(_make_request("Bearer correct-secret"))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["residency_policy"] == (policy or "simple")
    assert payload["queue_total"] == 7
    assert payload["logosnode"] == {"providers": {}}
