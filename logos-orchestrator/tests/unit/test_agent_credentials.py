"""Expiry, revocation and scope at the actual inference authentication boundary."""

import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

import logos as main
from logos import agent_credential, auth, batch_credential
from logos.anthropic_compat import web_search as server_web_search
from logos.routers import internal, user_facing

ROW = {
    "id": 17,
    "key_value": "internal-agent-not-a-bearer",
    "name": "agent",
    "key_type": "application",
    "team_id": None,
    "user_id": None,
    "environment": "agent",
    "log": "BILLING",
    "settings": {},
    "default_priority": 1,
    "internal_service": "agent",
}


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", "test-internal-secret")
    now = [1000]
    monkeypatch.setattr(agent_credential.time, "time", lambda: now[0])
    return now


@pytest.fixture
def database(monkeypatch):
    db = MagicMock()
    db.__enter__.return_value = db
    db.get_api_key_by_id.return_value = dict(ROW)
    db.get_agent_api_key_id.return_value = ROW["id"]
    db.get_api_key_by_value.return_value = None
    monkeypatch.setattr(auth, "DBManager", lambda: db)
    monkeypatch.setattr(internal, "DBManager", lambda: db)
    return db


def test_expiry_is_checked_on_every_call_and_new_token_renews(clock, database):
    token = agent_credential.issue_agent_credential(17)
    header = {"Authorization": f"Bearer {token}"}
    context = auth.authenticate_api_key(header)
    assert context.api_key_id == 17 and context.default_priority == 1
    assert context.team_id is None and context.user_role is None
    clock[0] += 59
    assert auth.authenticate_api_key(header).api_key_id == 17
    clock[0] += 1
    with pytest.raises(HTTPException) as exc:
        auth.authenticate_api_key(header)
    assert exc.value.status_code == 401
    renewed = agent_credential.issue_agent_credential(17)
    assert renewed != token
    assert auth.authenticate_api_key({"Authorization": f"Bearer {renewed}"}).api_key_id == 17


def test_identity_revocation_takes_effect_before_token_expires(clock, database):
    token = agent_credential.issue_agent_credential(17)
    headers = {"Authorization": f"Bearer {token}"}
    auth.authenticate_api_key(headers)
    database.get_api_key_by_id.return_value = None
    with pytest.raises(HTTPException):
        auth.authenticate_api_key(headers)


def test_cannot_use_agent_token_for_an_ordinary_identity(clock, database):
    database.get_api_key_by_id.return_value = {**ROW, "internal_service": None}
    with pytest.raises(HTTPException):
        auth.authenticate_api_key({"Authorization": f"Bearer {agent_credential.issue_agent_credential(17)}"})


def test_agent_token_is_not_a_batch_token(clock, database):
    token = agent_credential.issue_agent_credential(17)
    converted = token.replace(agent_credential.AGENT_CREDENTIAL_PREFIX, batch_credential.BATCH_CREDENTIAL_PREFIX, 1)
    assert batch_credential.resolve_batch_credential(converted) is None
    for credential in (token, converted):
        with pytest.raises(HTTPException):
            auth.authenticate_batch_api_key({"Authorization": f"Bearer {credential}"})


def test_tampering_and_secret_rotation_fail_closed(clock, monkeypatch):
    token = agent_credential.issue_agent_credential(17)
    assert agent_credential.resolve_agent_credential(token + "x") is None
    assert agent_credential.resolve_agent_credential(token.replace("ac1.", "bc1.")) is None
    monkeypatch.setenv("LOGOS_INTERNAL_SECRET", "rotated")
    assert agent_credential.resolve_agent_credential(token) is None
    monkeypatch.delenv("LOGOS_INTERNAL_SECRET")
    assert agent_credential.resolve_agent_credential(token) is None
    with pytest.raises(RuntimeError):
        agent_credential.issue_agent_credential(17)


@pytest.mark.parametrize("value", ["ac1.", "ac1...", "ac1.ß.ß", "ac1." + "x" * 1100, "not-a-token"])
def test_malformed_token_is_refused(value, clock):
    assert agent_credential.resolve_agent_credential(value) is None


def test_issuer_is_secret_gated_and_honors_revocation(clock, database, monkeypatch):
    monkeypatch.setattr(internal, "_INTERNAL_SECRET", "test-internal-secret")
    request = MagicMock(headers={"authorization": "Bearer wrong"})
    with pytest.raises(HTTPException) as exc:
        internal.internal_agent_credentials(request)
    assert exc.value.status_code == 401
    database.get_agent_api_key_id.assert_not_called()
    request.headers = {"authorization": "Bearer test-internal-secret"}
    response = internal.internal_agent_credentials(request)
    assert response.headers["cache-control"] == "no-store"
    assert agent_credential.resolve_agent_credential(response.headers["x-logos-agent-token"]) == 17
    assert b"internal-agent-not-a-bearer" not in response.body
    database.get_agent_api_key_id.return_value = None
    with pytest.raises(HTTPException) as exc:
        internal.internal_agent_credentials(request)
    assert exc.value.status_code == 403


def _turn(content, stop_reason):
    return {
        "id": "msg_upstream",
        "type": "message",
        "role": "assistant",
        "model": "local-model",
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


@pytest.fixture
def slow_web_search_turns(clock, database, monkeypatch):
    """Server-side web search whose every model turn outlasts one credential."""
    # The route resolves the auth module through sys.modules, not the
    # ``logos.auth`` attribute this file imports before the package handover.
    monkeypatch.setattr(sys.modules["logos.auth"], "DBManager", lambda: database)
    found = [{"title": "Docs", "url": "https://docs.example.org", "snippet": "docs"}]
    monkeypatch.setattr(server_web_search, "search_web", AsyncMock(return_value=found))
    search = {"type": "tool_use", "id": "toolu_1", "name": "web_search", "input": {"query": "docs"}}
    turns = [_turn([search], "tool_use"), _turn([{"type": "text", "text": "Answer"}], "end_turn")]
    authenticated = []

    async def handle(path, request):
        authenticated.append(user_facing.authenticate_api_key(dict(request.headers)).api_key_id)
        clock[0] += agent_credential.AGENT_CREDENTIAL_TTL_S + 1
        return JSONResponse(turns[len(authenticated) - 1])

    monkeypatch.setattr(user_facing, "handle_sync_request", handle)
    return authenticated


async def _post_web_search():
    payload = {
        "model": "local-model",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "Search for docs"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
    }
    headers = {"Authorization": f"Bearer {agent_credential.issue_agent_credential(17)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        return await client.post("/v1/messages", json=payload, headers=headers)


async def test_in_process_turns_renew_the_credential_of_a_long_request(slow_web_search_turns):
    response = await _post_web_search()
    assert response.status_code == 200
    assert [block["type"] for block in response.json()["content"]][-1] == "text"
    assert slow_web_search_turns == [17, 17]


async def test_in_process_turns_still_honor_identity_revocation(slow_web_search_turns, database):
    # Request authentication and the first turn see the identity; it is revoked before the second.
    database.get_api_key_by_id.side_effect = [dict(ROW), dict(ROW), None]
    response = await _post_web_search()
    assert response.status_code == 401
    assert slow_web_search_turns == [17]


def test_ordinary_keys_keep_their_credential_for_in_process_turns(clock):
    context = MagicMock(api_key_id=17)
    assert auth.issue_continuation_credential({"Authorization": "Bearer lg-key"}, context) is None
    assert auth.issue_continuation_credential({}, context) is None
