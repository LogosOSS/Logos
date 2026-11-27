"""Remote Agent settings are operator controlled, pinned, and credential-isolated."""

from dataclasses import replace
from hashlib import sha256
from unittest.mock import AsyncMock

import httpx
import pytest
from app import controls, db, docker_engine, inference_settings, main, sessions
from app.auth import Principal
from app.schemas import SessionStatus

PIN = {"inference_provider_id": 2, "inference_model_id": 8, "model": "cloud-model"}


@pytest.fixture
def remote_settings(monkeypatch):
    settings = replace(inference_settings.settings, remote_allowed_origins=("https://approved.example",))
    monkeypatch.setattr(inference_settings, "settings", settings)
    monkeypatch.setattr(sessions, "settings", settings)


async def test_remote_catalog_filters_origins_and_does_not_expose_endpoints(monkeypatch, remote_settings):
    rows = [
        {**PIN, "provider_id": 2, "provider_name": "Example", "model_id": 8, "base_url": "https://approved.example/"},
        {**PIN, "provider_id": 3, "base_url": "https://approved.example.evil/"},
        {**PIN, "provider_id": 4, "base_url": "https://approved.example:444/"},
        {**PIN, "provider_id": 5, "base_url": "http://approved.example/"},
        {**PIN, "provider_id": 6, "base_url": "https://user@approved.example/"},
        {**PIN, "provider_id": 7, "base_url": "https://approved.example:0/"},
        {**PIN, "provider_id": 8, "base_url": "https://approved.example/v1"},
        {**PIN, "provider_id": 9, "base_url": "https://approved.example:65536/"},
    ]
    monkeypatch.setattr(db, "agent_cloud_deployments", AsyncMock(return_value=rows))
    assert await inference_settings.cloud_options() == [
        {"provider_id": 2, "provider_name": "Example", "model_id": 8, "model": "cloud-model"}
    ]
    assert await inference_settings.permits_pin(PIN)
    assert not await inference_settings.permits_pin({**PIN, "model": "other"})
    assert not await inference_settings.permits_pin({**PIN, "inference_provider_id": None})


async def test_wrong_network_cannot_enable_remote_models(monkeypatch, remote_settings):
    monkeypatch.setattr(
        inference_settings,
        "settings",
        replace(
            inference_settings.settings, remote_inference_network=inference_settings.settings.session_egress_network
        ),
    )
    deployments = AsyncMock()
    monkeypatch.setattr(db, "agent_cloud_deployments", deployments)
    assert await inference_settings.cloud_options() == []
    deployments.assert_not_awaited()


async def test_non_admin_cannot_update_inference_settings(monkeypatch):
    main.app.dependency_overrides[main.require_agent_operator] = lambda: Principal(
        "actor", "operator", frozenset({"app_admin"})
    )
    save = AsyncMock()
    monkeypatch.setattr(inference_settings, "save", save)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            result = await client.put("/inference-settings", json={"mode": "cloud", "provider_id": 2, "model_id": 8})
    finally:
        main.app.dependency_overrides.pop(main.require_agent_operator)
    assert result.status_code == 403
    save.assert_not_awaited()


async def test_admin_saves_an_exact_allowed_model_and_no_endpoint(monkeypatch):
    monkeypatch.setattr(
        inference_settings,
        "cloud_options",
        AsyncMock(return_value=[{"provider_id": 2, "provider_name": "Example", "model_id": 8, "model": "cloud-model"}]),
    )
    save = AsyncMock()
    monkeypatch.setattr(db, "save_agent_inference_selection", save)
    main.app.dependency_overrides[main.require_agent_operator] = lambda: Principal(
        "actor", "operator", frozenset({"logos_admin"})
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            denied = await client.put(
                "/inference-settings", json={"mode": "cloud", "provider_id": 2, "model_id": 8, "model": "wrong"}
            )
            accepted = await client.put("/inference-settings", json={"mode": "cloud", "provider_id": 2, "model_id": 8})
    finally:
        main.app.dependency_overrides.pop(main.require_agent_operator)
    assert denied.status_code == 400
    assert accepted.status_code == 200
    save.assert_awaited_once_with("cloud", "cloud-model", 2, 8, "operator")


async def test_remote_grant_is_random_and_only_digest_is_stored(monkeypatch, remote_settings):
    store = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "set_agent_inference_grant", store)
    first = await inference_settings.issue_remote_grant(10)
    second = await inference_settings.issue_remote_grant(10)
    assert first and second and first != second and len(first) >= 43
    assert [call.args for call in store.await_args_list] == [
        (10, sha256(first.encode("ascii")).hexdigest()),
        (10, sha256(second.encode("ascii")).hexdigest()),
    ]
    env = sessions.SessionManager()._session_env({**PIN, "id": 10, "task": "code"}, "branch", inference_grant=first)
    assert env["ANTHROPIC_BASE_URL"] == "http://logos-agent-remote-gateway"
    assert env["ANTHROPIC_AUTH_TOKEN"] == first
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_MODEL"] == "cloud-model"
    with pytest.raises(ValueError, match="requires a gateway grant"):
        sessions.SessionManager()._session_env({**PIN, "id": 10, "task": "code"}, "branch")


async def test_remote_claim_does_not_query_local_gpu_capacity(monkeypatch, remote_settings):
    manager = sessions.SessionManager()
    local_pass = AsyncMock(side_effect=AssertionError("remote admission must not depend on local GPUs"))
    monkeypatch.setattr(manager, "_schedule_local_capacity", local_pass)
    monkeypatch.setattr(manager, "_honor_cancel_requests", AsyncMock())
    monkeypatch.setattr(manager, "_launch", AsyncMock())
    monkeypatch.setattr(controls, "current", AsyncMock(return_value=controls.Controls()))
    monkeypatch.setattr(db, "sessions_in_status", AsyncMock(return_value=[]))
    queue = AsyncMock(return_value={**PIN, "id": 10})
    monkeypatch.setattr(db, "next_queued_session", queue)
    claim = AsyncMock(return_value={**PIN, "id": 10})
    monkeypatch.setattr(db, "claim_session", claim)
    monkeypatch.setattr(db, "add_event", AsyncMock())

    await manager.scheduler_pass()
    queue.assert_awaited_once_with(remote=True)
    assert claim.await_args.kwargs["max_active"] > 0
    assert claim.await_args.kwargs["memory_mb"] > 0
    assert manager._launch.await_args.args[0]["inference_provider_id"] == 2
    local_pass.assert_not_awaited()


async def test_remote_resume_clears_the_binding_before_thawing(monkeypatch, remote_settings):
    manager = sessions.SessionManager()
    session = {**PIN, "id": 10, "container_id": "remote-container"}
    monkeypatch.setattr(manager, "_container_now", AsyncMock(return_value=("paused", None)))
    monkeypatch.setattr(inference_settings, "permits_pin", AsyncMock(return_value=True))
    called = []

    async def note(name, *args):
        called.append((name, *args))
        return True

    monkeypatch.setattr(db, "clear_agent_inference_client_tag", lambda *args: note("clear", *args))
    monkeypatch.setattr(db, "transition_session", lambda *args: note("state", *args))
    monkeypatch.setattr(db, "add_event", AsyncMock())
    monkeypatch.setattr(docker_engine, "connect_network", lambda *args: note("connect", *args))
    monkeypatch.setattr(docker_engine, "unpause_container", lambda *args: note("thaw", *args))

    assert await manager._resume(session, "continue")
    assert called == [
        ("clear", 10),
        ("connect", "logos-agent-remote-net", "remote-container"),
        ("state", 10, SessionStatus.RUNNING),
        ("thaw", "remote-container"),
    ]
