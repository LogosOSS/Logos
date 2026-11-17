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


def test_an_unconfigured_node_reads_as_revision_zero_without_entries():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(
        config_revision=None, yaml_imported_at=None, entries=[]
    )
    assert db.get_node_config(7) == {"revision": 0, "yaml_imported": False, "entries": []}


def test_a_configured_node_reads_its_revision_and_models():
    db = _db()
    entries = [{"model_name": "org/a", "capability": {"tensor_parallel_size": 2}, "overrides": {}}]
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(
        config_revision=5, yaml_imported_at="t", entries=entries
    )
    assert db.get_node_config(7) == {"revision": 5, "yaml_imported": True, "entries": entries}


def test_a_node_is_read_in_one_statement_in_list_order():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(
        config_revision=1, yaml_imported_at=None, entries=[]
    )
    db.get_node_config(7)
    assert db.session.execute.call_count == 1
    source = inspect.getsource(DBManager.get_node_config)
    assert "ORDER BY position, id" in source and "LEFT JOIN node_config_state" in source


def test_a_known_user_id_exists_and_an_unknown_one_does_not():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace()
    assert db.user_exists(5) is True
    assert db.session.execute.call_args.args[1] == {"uid": 5}
    db.session.execute.return_value.fetchone.return_value = None
    assert db.user_exists(6) is False


def test_the_sources_pairs_and_runs_are_read_as_maps():
    import datetime

    db = _db()
    db.session.execute.return_value.fetchall.return_value = [SimpleNamespace(model_name="org/a", source="ui")]
    assert db.get_node_config_sources(7) == {"org/a": "ui"}
    db.session.execute.return_value.fetchall.return_value = [SimpleNamespace(name="org/a", id=11)]
    assert db.get_model_provider_pairs(7) == {"org/a": 11}
    when = datetime.datetime(2026, 11, 1)
    db.session.execute.return_value.fetchall.return_value = [
        SimpleNamespace(id=2, serving={"tensor_parallel_size": 2}, recorded_at=when, ttft_ms="470.0", latency_ms=5500),
        SimpleNamespace(id=3, serving=None, recorded_at=None, ttft_ms=None, latency_ms="x"),
    ]
    assert db.get_benchmark_runs([2, 3]) == {
        2: {"serving": {"tensor_parallel_size": 2}, "recorded_at": when.isoformat(), "ttft_s": 0.47, "latency_s": 5.5},
        3: {"serving": {}, "recorded_at": None, "ttft_s": None, "latency_s": None},
    }


def test_no_run_ids_ask_the_database_nothing():
    db = _db()
    assert db.get_benchmark_runs([]) == {}
    db.session.execute.assert_not_called()
