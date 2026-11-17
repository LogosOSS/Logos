"""Calibrating one model of a node, called by the webservice."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import logos as main_mod
from logos.dbutils.dbrequest import InternalCalibrateRequest
from logos.logosnode_registry import LogosNodeCommandError, LogosNodeOfflineError
from logos.routers import internal as internal_mod


def _request() -> MagicMock:
    request = MagicMock()
    request.headers.get = lambda key, default="": "Bearer secret" if key == "authorization" else default
    return request


def _node(monkeypatch, send=None, snap=None, needing=("org/a", "org/b")) -> MagicMock:
    monkeypatch.setattr(internal_mod, "_INTERNAL_SECRET", "secret")
    monkeypatch.setattr(internal_mod.benchmark_reservations, "owner", lambda *_a: None)
    registry = MagicMock()
    registry.peek_runtime_snapshot = lambda pid: snap if snap is not None else {"first_status_received": True}
    registry.send_command = send or AsyncMock(return_value={})
    monkeypatch.setattr(main_mod, "_logosnode_registry", registry)
    monkeypatch.setattr(main_mod, "_calibration_orchestrator", None)
    monkeypatch.setattr(internal_mod, "_find_uncalibrated_models_on_provider", lambda pid: list(needing))
    monkeypatch.setattr(internal_mod, "_resolve_provider_name", lambda pid: "node")
    return registry


@pytest.mark.asyncio
async def test_without_a_model_the_session_calibrates_everything_that_needs_it(monkeypatch):
    registry = _node(monkeypatch)

    result = await internal_mod.internal_logosnode_calibrate_uncalibrated(
        InternalCalibrateRequest(provider_id=1), _request()
    )

    assert result["models"] == ["org/a", "org/b"]
    assert "models" not in registry.send_command.call_args.kwargs["params"]


@pytest.mark.asyncio
async def test_with_a_model_only_that_model_is_sent_to_the_worker(monkeypatch):
    registry = _node(monkeypatch)

    result = await internal_mod.internal_logosnode_calibrate_uncalibrated(
        InternalCalibrateRequest(provider_id=1, model="org/b"), _request()
    )

    assert result["models"] == ["org/b"]
    assert registry.send_command.call_args.kwargs["params"]["models"] == ["org/b"]


@pytest.mark.asyncio
async def test_a_model_that_needs_no_calibration_starts_nothing(monkeypatch):
    registry = _node(monkeypatch)

    result = await internal_mod.internal_logosnode_calibrate_uncalibrated(
        InternalCalibrateRequest(provider_id=1, model="org/done"), _request()
    )

    assert result["count"] == 0
    assert "org/done does not need calibration" in result["message"]
    registry.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_an_offline_worker_is_a_503_and_a_refusal_a_409(monkeypatch):
    _node(monkeypatch, send=AsyncMock(side_effect=LogosNodeOfflineError("gone")))
    offline = await internal_mod.internal_logosnode_calibrate_uncalibrated(
        InternalCalibrateRequest(provider_id=1, model="org/a"), _request()
    )
    _node(monkeypatch, send=AsyncMock(side_effect=LogosNodeCommandError("busy")))
    refused = await internal_mod.internal_logosnode_calibrate_uncalibrated(
        InternalCalibrateRequest(provider_id=1, model="org/a"), _request()
    )

    assert (offline.status_code, refused.status_code) == (503, 409)
