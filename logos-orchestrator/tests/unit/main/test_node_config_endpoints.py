"""Admin and worker endpoints of the central node model configuration."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from logos.dbutils.dbrequest import (
    LogosNodeConfigGetRequest,
    LogosNodeConfigImportRequest,
    LogosNodeConfigReplaceRequest,
    LogosNodeConfigRequest,
    NodeConfigEntry,
)
from logos.logosnode_registry import LogosNodeCommandError, LogosNodeOfflineError
from logos.node_config import SYNC_NODE_CONFIG_ACTION, VALIDATE_NODE_CONFIG_ACTION
from logos.routers import logosnode as logosnode_mod

_STORED = {
    "revision": 3,
    "yaml_imported": True,
    "entries": [{"model_name": "org/a", "capability": {}, "overrides": {}}],
}


class _FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeDB:
    def __init__(self, *, provider_exists=True, new_revision=4, claim=True):
        self.session = _FakeSession()
        self.provider_exists = provider_exists
        self.new_revision = new_revision
        self.claim = claim
        self.replaced: list = []
        self.checked_keys: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_provider(self, provider_id):
        return {"id": provider_id} if self.provider_exists else None

    def get_node_config(self, provider_id):
        return dict(_STORED)

    def replace_node_config(self, provider_id, entries, expected_revision, updated_by, source):
        self.replaced.append((provider_id, entries, expected_revision, updated_by, source))
        return self.new_revision

    def claim_yaml_import(self, provider_id):
        return self.claim

    def get_user_by_api_key(self, key):
        return {"id": 42}


class _FakeRegistry:
    def __init__(self, actions=(SYNC_NODE_CONFIG_ACTION, VALIDATE_NODE_CONFIG_ACTION), fail=False, connected=True):
        self.actions = set(actions)
        self.fail = fail
        self.connected = connected
        self.validation = {"ok": True, "errors": []}
        self.validate_error: Exception | None = None
        self.sent: list = []
        self.validated: list = []

    def peek_runtime_snapshot(self, provider_id):
        return {} if self.connected else None

    def supports_action(self, provider_id, action):
        return action in self.actions

    async def send_command(self, provider_id, action, params=None, timeout_seconds=20):
        if action == VALIDATE_NODE_CONFIG_ACTION:
            if self.validate_error is not None:
                raise self.validate_error
            self.validated.append((provider_id, params))
            return self.validation
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append((provider_id, action, params))
        return {"ok": True}


@pytest.fixture
def env(monkeypatch):
    """Patch the database, the registry and the auth helpers of the router."""

    def _setup(db=None, registry=None):
        db = db or _FakeDB()
        registry = registry or _FakeRegistry()
        monkeypatch.setattr(logosnode_mod, "DBManager", lambda: db)
        monkeypatch.setattr(logosnode_mod._main, "_logosnode_registry", registry)
        monkeypatch.setattr(logosnode_mod, "_require_tls_request", lambda request: None)
        monkeypatch.setattr(logosnode_mod, "_logosnode_provider_for_key", lambda key: {"id": 5})
        monkeypatch.setattr(logosnode_mod, "_require_root_access", lambda key: db.checked_keys.append(key))
        monkeypatch.setattr(logosnode_mod, "require_logos_admin_key", lambda key, db_: db.checked_keys.append(key))
        monkeypatch.setattr(logosnode_mod, "_resolve_provider_name", lambda pid: f"gpu-{pid}")
        return db, registry

    return _setup


def _entry(name="org/a", capability=None, overrides=None):
    return NodeConfigEntry(model_name=name, capability=capability or {}, overrides=overrides or {})


def _body(response):
    return response.status_code, json.loads(response.body)


def test_a_worker_reads_the_stored_configuration_of_its_node(env):
    env()
    result = asyncio.run(logosnode_mod.logosnode_node_config(LogosNodeConfigRequest(shared_key="s"), request=None))
    assert result == _STORED


def test_the_first_import_stores_the_yaml_models_and_reports_conflicts(env):
    db, _ = env()
    data = LogosNodeConfigImportRequest(
        shared_key="s",
        entries=[_entry(" org/a ", {"tensor_parallel_size": 2}, {"tensor_parallel_size": 4})],
    )
    result = asyncio.run(logosnode_mod.logosnode_node_config_import(data, request=None))
    assert result["imported"] is True
    assert len(result["warnings"]) == 1
    provider_id, rows, expected, updated_by, source = db.replaced[0]
    assert (provider_id, expected, updated_by, source) == (5, 0, None, "yaml_import")
    assert rows[0]["model_name"] == "org/a"
    assert db.session.commits == 1


def test_a_later_import_changes_nothing(env):
    db, _ = env(_FakeDB(claim=False))
    data = LogosNodeConfigImportRequest(shared_key="s", entries=[_entry()])
    result = asyncio.run(logosnode_mod.logosnode_node_config_import(data, request=None))
    assert result == {"imported": False, "warnings": []}
    assert db.replaced == []
    assert db.session.rollbacks == 1


def test_an_import_with_repeated_names_is_rejected(env):
    db, _ = env()
    data = LogosNodeConfigImportRequest(shared_key="s", entries=[_entry("org/a"), _entry("org/a ")])
    status, body = _body(asyncio.run(logosnode_mod.logosnode_node_config_import(data, request=None)))
    assert status == 400
    assert "listed twice" in body["error"]
    assert db.replaced == []


def test_an_admin_reads_a_nodes_configuration(env):
    db, _ = env()
    result = asyncio.run(
        logosnode_mod.logosnode_node_config_get(LogosNodeConfigGetRequest(logos_key="k", provider_id=7))
    )
    assert result == {"provider_id": 7, **_STORED}
    assert db.checked_keys == ["k"]


def test_reading_an_unknown_node_answers_404(env):
    env(_FakeDB(provider_exists=False))
    response = asyncio.run(
        logosnode_mod.logosnode_node_config_get(LogosNodeConfigGetRequest(logos_key="k", provider_id=7))
    )
    assert response.status_code == 404


def _replace(entries=None, expected_revision=3):
    data = LogosNodeConfigReplaceRequest(
        logos_key="k",
        provider_id=7,
        expected_revision=expected_revision,
        entries=[_entry()] if entries is None else entries,
    )
    return asyncio.run(logosnode_mod.logosnode_node_config_replace(data))


def test_a_saved_configuration_is_stored_and_pushed_to_the_worker(env):
    db, registry = env()
    assert _replace() == {"provider_id": 7, "config_revision": 4}
    assert db.replaced[0][3:] == (42, "ui")
    assert db.session.commits == 1
    assert registry.sent == [(7, SYNC_NODE_CONFIG_ACTION, _STORED)]


def test_an_invalid_configuration_is_rejected_before_anything_is_written(env):
    db, _ = env()
    status, body = _body(_replace([_entry(capability={"tensor_parallel_size": 0})]))
    assert status == 400
    assert "tensor_parallel_size" in body["error"]
    assert db.replaced == []


def test_saving_for_an_unknown_node_answers_404(env):
    db, _ = env(_FakeDB(provider_exists=False))
    assert _replace().status_code == 404
    assert db.replaced == []


def test_an_outdated_revision_answers_409_with_the_current_one(env):
    db, registry = env(_FakeDB(new_revision=None))
    status, body = _body(_replace(expected_revision=1))
    assert status == 409
    assert "revision 3" in body["error"]
    assert db.session.rollbacks == 1
    assert registry.sent == []


def test_a_worker_without_the_sync_action_is_not_sent_the_configuration(env):
    _, registry = env(registry=_FakeRegistry(actions={VALIDATE_NODE_CONFIG_ACTION}))
    assert _replace()["config_revision"] == 4
    assert registry.sent == []


def test_a_failed_push_still_answers_with_the_saved_revision(env):
    env(registry=_FakeRegistry(fail=True))
    assert _replace()["config_revision"] == 4


def test_the_worker_validates_the_models_before_they_are_stored(env):
    db, registry = env()
    _replace([_entry("org/a", {"tensor_parallel_size": 2}, {"quantization": "awq"})])
    ((provider_id, params),) = registry.validated
    assert provider_id == 7
    assert params == {
        "entries": [
            {"model_name": "org/a", "capability": {"tensor_parallel_size": 2}, "overrides": {"quantization": "awq"}}
        ]
    }
    assert len(db.replaced) == 1


def test_what_the_worker_refuses_is_not_stored(env):
    db, registry = env()
    registry.validation = {"ok": False, "errors": ["org/a: dtype: bad", "org/b: x: bad"]}
    status, body = _body(_replace())
    assert status == 400
    assert body["error"] == "org/a: dtype: bad; org/b: x: bad"
    assert db.replaced == []


def test_a_refusal_without_a_reason_still_answers_400(env):
    _, registry = env()
    registry.validation = {"ok": False}
    status, body = _body(_replace())
    assert status == 400 and "refused" in body["error"]


def test_an_offline_worker_cannot_validate_so_nothing_is_stored(env):
    db, _ = env(registry=_FakeRegistry(connected=False))
    status, body = _body(_replace())
    assert (status, body["error"]) == (503, "Worker not connected")
    assert db.replaced == []


def test_a_worker_that_drops_out_meanwhile_answers_503(env):
    db, registry = env()
    registry.validate_error = LogosNodeOfflineError("gone")
    assert _replace().status_code == 503
    assert db.replaced == []


def test_a_worker_that_cannot_answer_the_validation_answers_502(env):
    db, registry = env()
    registry.validate_error = LogosNodeCommandError("timed out")
    status, body = _body(_replace())
    assert status == 502 and "timed out" in body["error"]
    assert db.replaced == []


def test_an_old_worker_without_validation_is_refused_with_a_hint(env):
    db, _ = env(registry=_FakeRegistry(actions={SYNC_NODE_CONFIG_ACTION}))
    status, body = _body(_replace())
    assert status == 409 and "update" in body["error"]
    assert db.replaced == []


def test_the_unknown_node_is_reported_before_any_worker_is_asked(env):
    _, registry = env(_FakeDB(provider_exists=False))
    assert _replace().status_code == 404
    assert registry.validated == []


def test_clearing_a_nodes_models_needs_no_worker(env):
    db, registry = env(registry=_FakeRegistry(connected=False))
    assert _replace(entries=[], expected_revision=3)["config_revision"] == 4
    assert registry.validated == []
    assert db.replaced[0][1] == []


def test_a_hello_schedules_the_configuration_push(env):
    _, registry = env()

    async def _run():
        logosnode_mod._schedule_node_config_push(7)
        await asyncio.gather(*list(logosnode_mod._main._background_tasks))

    asyncio.run(_run())
    assert registry.sent == [(7, SYNC_NODE_CONFIG_ACTION, _STORED)]
    assert "_schedule_node_config_push(ticket.provider_id)" in inspect.getsource(logosnode_mod.logosnode_session)
