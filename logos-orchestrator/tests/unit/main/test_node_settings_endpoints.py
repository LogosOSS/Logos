"""Node-wide settings: the worker's read and import, the admin's save and the push."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from logos.dbutils.dbrequest import (
    InternalNodeConfigRequest,
    InternalNodeSettingsReplaceRequest,
    LogosNodeConfigRequest,
    LogosNodeSettingsImportRequest,
)
from logos.node_settings import SYNC_NODE_SETTINGS_ACTION
from logos.routers import internal as internal_mod
from logos.routers import logosnode as logosnode_mod

_STORED = {"revision": 3, "yaml_imported": True, "settings": {"gpu_devices": "0,1"}}


class _Session:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _DB:
    def __init__(self, provider_exists=True, new_revision=4, claim=True, known_users=(42,)):
        self.session = _Session()
        self.provider_exists = provider_exists
        self.new_revision = new_revision
        self.claim = claim
        self.known_users = set(known_users)
        self.replaced = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_provider(self, provider_id):
        return {"id": provider_id} if self.provider_exists else None

    def get_node_settings(self, provider_id):
        return dict(_STORED)

    def replace_node_settings(self, provider_id, settings, expected_revision, updated_by):
        self.replaced.append((provider_id, settings, expected_revision, updated_by))
        return self.new_revision

    def claim_settings_import(self, provider_id):
        return self.claim

    def user_exists(self, user_id):
        return user_id in self.known_users


class _Registry:
    def __init__(self, supported=True, fail=False):
        self.supported = supported
        self.fail = fail
        self.sent = []

    def supports_action(self, provider_id, action):
        return self.supported and action == SYNC_NODE_SETTINGS_ACTION

    async def send_command(self, provider_id, action, params=None, timeout_seconds=20):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append((provider_id, action, params))
        return {"ok": True}


@pytest.fixture
def env(monkeypatch):
    def _setup(db=None, registry=None):
        db = db or _DB()
        registry = registry or _Registry()
        monkeypatch.setattr(logosnode_mod, "DBManager", lambda: db)
        monkeypatch.setattr(logosnode_mod._main, "_logosnode_registry", registry)
        monkeypatch.setattr(logosnode_mod, "_require_tls_request", lambda request: None)
        monkeypatch.setattr(logosnode_mod, "_logosnode_provider_for_key", lambda key: {"id": 5})
        monkeypatch.setattr(logosnode_mod, "_resolve_provider_name", lambda pid: f"gpu-{pid}")
        monkeypatch.setattr(internal_mod, "_require_internal_secret", lambda request, **_: None)
        return db, registry

    return _setup


def _body(response):
    return response.status_code, json.loads(response.body)


def _save(settings, expected_revision=3, user_id=42):
    data = InternalNodeSettingsReplaceRequest(
        provider_id=7, user_id=user_id, expected_revision=expected_revision, settings=settings
    )
    return asyncio.run(internal_mod.internal_logosnode_node_settings_replace(data, request=None))


def test_a_worker_reads_the_stored_settings_of_its_node(env):
    env()

    result = asyncio.run(logosnode_mod.logosnode_node_settings(LogosNodeConfigRequest(shared_key="s"), request=None))

    assert result == _STORED


def test_the_first_import_keeps_the_known_yaml_values_only(env):
    db, _ = env()
    data = LogosNodeSettingsImportRequest(shared_key="s", settings={"gpu_devices": "all", "other": 1})

    result = asyncio.run(logosnode_mod.logosnode_node_settings_import(data, request=None))

    assert result == {"imported": True}
    assert db.replaced == [(5, {"gpu_devices": "all"}, 0, None)]
    assert db.session.commits == 1


def test_a_second_import_changes_nothing(env):
    db, _ = env(_DB(claim=False))

    result = asyncio.run(
        logosnode_mod.logosnode_node_settings_import(
            LogosNodeSettingsImportRequest(shared_key="s", settings={"gpu_devices": "all"}), request=None
        )
    )

    assert result == {"imported": False}
    assert db.replaced == []
    assert db.session.rollbacks == 1


def test_a_valid_save_stores_the_settings_and_pushes_them(env):
    db, registry = env()

    result = _save({"gpu_devices": "0", "gpu_poll_interval": 10})

    assert result == {"provider_id": 7, "settings_revision": 4}
    assert db.replaced == [(7, {"gpu_devices": "0", "gpu_poll_interval": 10}, 3, 42)]
    assert registry.sent == [(7, SYNC_NODE_SETTINGS_ACTION, _STORED)]


def test_a_refused_value_is_a_400_with_the_problems_by_key(env):
    db, registry = env()

    status, body = _body(_save({"gpu_devices": "left", "gpu_poll_interval": 0, "colour": "red"}))

    assert status == 400
    assert {item["key"] for item in body["errors"]} == {"gpu_devices", "gpu_poll_interval", "colour"}
    assert db.replaced == []
    assert registry.sent == []


def test_a_stale_revision_is_a_409_that_names_the_current_one(env):
    _, registry = env(_DB(new_revision=None))

    status, body = _body(_save({"gpu_devices": "all"}, expected_revision=1))

    assert status == 409
    assert "revision 3" in body["error"]
    assert registry.sent == []


def test_an_unknown_node_is_a_404(env):
    env(_DB(provider_exists=False))

    assert _body(_save({"gpu_devices": "all"}))[0] == 404


def test_an_author_the_database_does_not_know_is_not_stored(env):
    db, _ = env()

    _save({"gpu_devices": "all"}, user_id=999)

    assert db.replaced[0][3] is None


def test_the_worker_keeps_its_settings_when_the_push_fails_or_is_unsupported(env):
    _, failing = env(registry=_Registry(fail=True))
    assert _save({"gpu_devices": "all"}) == {"provider_id": 7, "settings_revision": 4}

    _, old = env(registry=_Registry(supported=False))
    assert _save({"gpu_devices": "all"}) == {"provider_id": 7, "settings_revision": 4}
    assert old.sent == []


def test_the_internal_read_answers_the_settings_or_a_404(env):
    env()
    data = InternalNodeConfigRequest(provider_id=7)

    assert asyncio.run(internal_mod.internal_logosnode_node_settings_get(data, request=None)) == {
        "provider_id": 7,
        **_STORED,
    }

    env(_DB(provider_exists=False))
    assert _body(asyncio.run(internal_mod.internal_logosnode_node_settings_get(data, request=None)))[0] == 404


def test_a_worker_gets_the_settings_after_every_hello():
    assert "_schedule_node_settings_push(ticket.provider_id)" in inspect.getsource(logosnode_mod.logosnode_session)


def test_an_import_leaves_out_a_setting_that_is_too_large(env):
    db, _ = env()
    huge = ["--flag"] * 20000
    data = LogosNodeSettingsImportRequest(shared_key="s", settings={"global_extra_args": huge, "gpu_devices": "all"})

    result = asyncio.run(logosnode_mod.logosnode_node_settings_import(data, request=None))

    assert result == {"imported": True}
    assert db.replaced == [(5, {"gpu_devices": "all"}, 0, None)]
