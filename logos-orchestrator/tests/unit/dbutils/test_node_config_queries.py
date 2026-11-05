"""Revision and import guards of the central node model configuration."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

from logos.dbutils.dbmanager import DBManager

_ENTRIES = [
    {"model_name": "org/a", "capability": {"tensor_parallel_size": 2}, "overrides": {}},
    {"model_name": "org/b", "capability": {}, "overrides": {"quantization": "awq"}},
]


def _db():
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    return db


def test_a_stale_revision_writes_no_models():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = None
    assert db.replace_node_config(7, _ENTRIES, 3, 1, "ui") is None
    # Only the state row is ensured and tried: no delete, no inserts
    assert db.session.execute.call_count == 2


def test_replace_returns_the_new_revision_and_keeps_the_list_order():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(config_revision=4)
    assert db.replace_node_config(7, _ENTRIES, 3, 1, "ui") == 4
    rows = [c.args[1] for c in db.session.execute.call_args_list if "position" in c.args[1]]
    assert [(r["model_name"], r["position"]) for r in rows] == [("org/a", 0), ("org/b", 1)]


def test_the_yaml_import_is_only_granted_once():
    db = _db()
    db.session.execute.return_value.rowcount = 1
    assert db.claim_yaml_import(7) is True
    db.session.execute.return_value.rowcount = 0
    assert db.claim_yaml_import(7) is False
    source = inspect.getsource(DBManager.claim_yaml_import)
    assert "yaml_imported_at IS NULL" in source and "NOT EXISTS" in source
