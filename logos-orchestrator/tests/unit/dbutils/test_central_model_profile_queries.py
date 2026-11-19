"""Guards in the central model-profile queries."""

from __future__ import annotations

import datetime
import inspect
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from logos.dbutils.dbmanager import (
    _CALIBRATED_PROFILE_COLUMNS,
    _CENTRAL_PROFILE_ROW,
    _PERSIST_CENTRAL_PROFILE_SQL,
    _RESET_PROFILE_COLUMNS,
    DBManager,
    _model_profile_column_params,
)
from logos.dbutils.dbrequest import LogosNodeModelProfilesRequest


def _db():
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    return db


def test_echo_is_only_applied_at_the_stored_revision():
    db = _db()
    db.session.execute.return_value = SimpleNamespace(rowcount=0)
    applied = db.persist_central_model_profile(
        7,
        "org/model",
        {"base_residency_mb": 1.0},
        {"base_residency_mb": 1.0, "max_context_length": 131072},
        3,
        "H1",
    )
    assert applied is False
    _sql, params = db.session.execute.call_args.args
    # Checked against the source: the unit-test conftest stubs sqlalchemy.
    assert _PERSIST_CENTRAL_PROFILE_SQL.endswith("WHERE model_profiles.sync_revision = EXCLUDED.sync_revision")
    assert params["sync_revision"] == 3
    assert json.loads(params["profile"]) == {"base_residency_mb": 1.0}
    assert params["max_reported_context_length"] == 131072


def test_echo_without_a_key_hash_keeps_the_reported_one():
    assert (
        "COALESCE(EXCLUDED.calibration_key_hash, model_profiles.calibration_key_hash)" in _PERSIST_CENTRAL_PROFILE_SQL
    )


def test_upsert_sql_guards_and_keeps_the_context_high_water_mark():
    sql = _PERSIST_CENTRAL_PROFILE_SQL
    assert sql.endswith("WHERE model_profiles.sync_revision = EXCLUDED.sync_revision")
    assert "max_reported_context_length = GREATEST(COALESCE(model_profiles.max_reported_context_length, 0)" in sql
    assert "profile = EXCLUDED.profile ||" in sql
    assert ":sync_revision" in sql
    assert "sync_revision = EXCLUDED" not in sql.split(" WHERE ")[0]


def test_known_calibration_is_linked_without_a_new_snapshot():
    db = _db()
    select_result = MagicMock()
    select_result.scalar_one.return_value = 11
    db.session.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=None)),
        select_result,
        MagicMock(fetchone=MagicMock(return_value=None)),
    ]
    revision = db.record_model_calibration(
        7,
        "org/model",
        {"base_residency_mb": 1.0},
        {"backend": "cuda"},
        "H1",
        datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc),
    )
    assert revision is None
    _sql, link_params = db.session.execute.call_args_list[2].args
    assert "calibration_id IS DISTINCT FROM :calibration_id" in inspect.getsource(DBManager._link_calibration)
    assert link_params["calibration_id"] == 11


def test_profile_emptied_by_a_central_change_stays_central():
    """Clearing the only verdict of an unsupported-only profile leaves {}
    at a bumped revision; the worker must still be sent that empty state."""
    assert "OR mp.sync_revision > 0" in _CENTRAL_PROFILE_ROW
    assert "_CENTRAL_PROFILE_ROW" in inspect.getsource(DBManager.get_central_model_profiles)


def test_malformed_worker_values_bind_as_null_instead_of_failing():
    params = _model_profile_column_params(
        {
            "base_residency_mb": "lots",
            "measurement_count": "n/a",
            "tensor_parallel_size": "2",
            "engine": 3,
            "last_measured_epoch": 1e300,
        }
    )
    assert params["base_residency_mb"] is None
    assert params["measurement_count"] == 0
    assert params["tensor_parallel_size"] == 2
    assert params["engine"] is None
    assert params["last_measured_at"] is None


def test_profile_requests_are_bounded():
    with pytest.raises(ValidationError):
        LogosNodeModelProfilesRequest(shared_key="k", calibration_key_hashes={str(i): "h" for i in range(1001)})


def test_reset_is_a_revision_bump_that_keeps_the_context_high_water_mark():
    source = inspect.getsource(DBManager.reset_model_profiles)
    assert "sync_revision = sync_revision + 1" in source
    assert "DELETE" not in source
    assert "max_reported_context_length" not in _RESET_PROFILE_COLUMNS
    assert {"base_residency_mb", "residency_source", "last_measured_at"} <= set(_RESET_PROFILE_COLUMNS)


def test_reported_key_hashes_are_updated_in_one_statement():
    db = _db()
    db.update_reported_calibration_keys(7, {"a": "H1", "b": "H2"}, {"a": 2}, {"b": "F2"})
    assert db.session.execute.call_count == 1
    _sql, params = db.session.execute.call_args.args
    assert params == {
        "provider_id": 7,
        "model_names": ["a", "b"],
        "key_hashes": ["H1", "H2"],
        "slice_counts": [2, None],
        "fingerprints": [None, "F2"],
        "reports_fingerprints": True,
    }


def test_a_worker_without_fingerprints_leaves_the_stored_ones_alone():
    db = _db()
    db.update_reported_calibration_keys(7, {"a": "H1"}, None, None)
    _sql, params = db.session.execute.call_args.args
    assert params["reports_fingerprints"] is False


def test_no_reported_key_hashes_cost_no_round_trip():
    db = _db()
    db.update_reported_calibration_keys(7, {})
    db.session.execute.assert_not_called()


def test_an_echo_keeps_the_stored_values_of_overridden_fields():
    assert "FROM jsonb_each(model_profiles.profile)" in _PERSIST_CENTRAL_PROFILE_SQL
    assert ":overridden_fields" in _PERSIST_CENTRAL_PROFILE_SQL


def test_an_implausible_gpu_count_is_dropped_instead_of_failing_the_report():
    db = _db()
    db.update_reported_calibration_keys(7, {"a": "H1", "b": "H2", "c": "H3"}, {"a": 10**12, "b": -1, "c": 4})
    _sql, params = db.session.execute.call_args.args
    assert params["slice_counts"] == [None, None, 4]


def test_a_settled_node_costs_one_query_to_find_nothing_to_adopt():
    db = _db()
    db.session.execute.return_value.fetchall.return_value = []
    assert db.adopt_shared_calibrations(7, ["a", "b"]) == []
    assert db.session.execute.call_count == 1


def test_no_models_cost_no_query():
    db = _db()
    assert db.adopt_shared_calibrations(7, []) == []
    db.session.execute.assert_not_called()


def test_an_old_snapshot_is_still_adopted():
    assert "calibrated_at >=" not in inspect.getsource(DBManager._adopt_shared_calibration)


def test_the_profile_changes_leave_the_commit_to_the_caller():
    calls = (
        lambda db: db.update_reported_calibration_keys(7, {"a": "H"}),
        lambda db: db.adopt_shared_calibrations(7, ["a"]),
        lambda db: db.adopt_shared_calibration_for_siblings(7, "a"),
        lambda db: db.clear_calibration_unsupported(7, "a"),
        lambda db: db.reset_model_profiles(7),
        lambda db: db.invalidate_model_calibration(1, "bad"),
        lambda db: db.reject_shared_calibration(7, "a", 1),
    )
    for call in calls:
        db = _db()
        db.session.execute.return_value.fetchall.return_value = []
        db.session.execute.return_value.fetchone.return_value = None
        call(db)
        db.session.commit.assert_not_called()


def test_the_columns_a_calibration_clears_are_typed_columns():
    assert set(_CALIBRATED_PROFILE_COLUMNS) <= set(_model_profile_column_params({}))


def _candidate(snapshot_id, **profile):
    return SimpleNamespace(id=snapshot_id, profile=json.dumps(profile))


def _hint(fingerprint=None):
    return SimpleNamespace(calibration_key_hash="K", slice_gpu_count=4, config_fingerprint=fingerprint)


def _results(candidates, selection, locked_ids):
    """Session results in call order: candidates, the node's choice, then one lock per attempt."""
    answers = [
        SimpleNamespace(fetchall=lambda: candidates),
        SimpleNamespace(fetchone=lambda: selection),
    ]
    for snapshot_id in locked_ids:
        answers.append(
            SimpleNamespace(fetchone=lambda snapshot_id=snapshot_id: SimpleNamespace(id=snapshot_id, profile="{}"))
        )
    return answers


def test_adoption_prefers_the_snapshot_of_the_nodes_configuration_in_the_query():
    db = _db()
    db.session.execute.return_value = SimpleNamespace(fetchall=lambda: [])

    assert db._pick_shared_snapshot(7, "org/m", _hint("FP")) is None

    _sql, params = db.session.execute.call_args.args
    # Checked against the source: the unit-test conftest stubs sqlalchemy.
    source = inspect.getsource(DBManager._pick_shared_snapshot)
    # A calibration without a fingerprint makes the expression NULL, which PostgreSQL sorts first for DESC:
    # it must come after an exact match, not before.
    assert "profile->>'config_fingerprint' = CAST(:fingerprint AS text)) DESC NULLS LAST" in source
    assert params["fingerprint"] == "FP"
    assert params["key_hash"] == "K"


def test_adoption_takes_the_first_candidate_that_covers_the_nodes_choice():
    db = _db()
    narrow = _candidate(1, min_kv_cache_mb=1024.0, max_kv_cache_mb=2048.0)
    wide = _candidate(2, min_kv_cache_mb=1024.0, max_kv_cache_mb=8192.0)
    selection = SimpleNamespace(capability={"kv_cache_memory_bytes": "4G"}, overrides={})
    db.session.execute.side_effect = _results([narrow, wide], selection, [2])

    picked = db._pick_shared_snapshot(7, "org/m", _hint())

    assert picked.id == 2


def test_adoption_takes_nothing_when_no_candidate_covers_the_choice():
    db = _db()
    narrow = _candidate(1, min_kv_cache_mb=1024.0, max_kv_cache_mb=2048.0)
    db.session.execute.side_effect = _results(
        [narrow], SimpleNamespace(capability={"kv_cache_memory_bytes": "8G"}, overrides={}), []
    )

    assert db._pick_shared_snapshot(7, "org/m", _hint()) is None


def test_adoption_skips_a_snapshot_invalidated_before_it_was_locked():
    db = _db()
    first, second = _candidate(1), _candidate(2)
    answers = [
        SimpleNamespace(fetchall=lambda: [first, second]),
        SimpleNamespace(fetchone=lambda: None),
        SimpleNamespace(fetchone=lambda: None),
        SimpleNamespace(fetchone=lambda: SimpleNamespace(id=2, profile="{}")),
    ]
    db.session.execute.side_effect = answers

    assert db._pick_shared_snapshot(7, "org/m", _hint()).id == 2


def test_adoption_without_a_configured_choice_takes_the_first_candidate():
    db = _db()
    db.session.execute.side_effect = _results([_candidate(1, max_kv_cache_mb=1.0)], None, [1])

    assert db._pick_shared_snapshot(7, "org/m", _hint()).id == 1


def test_the_nodes_choice_is_read_from_its_configuration():
    db = _db()
    row = SimpleNamespace(capability={"kv_cache_memory_bytes": "2G"}, overrides={"max_model_len": 8192})
    db.session.execute.return_value = SimpleNamespace(fetchone=lambda: row)
    assert db._node_selection(7, "org/m") == (2048.0, 8192, None)

    # Text from the driver, and a choice that is not one.
    text_row = SimpleNamespace(capability='{"kv_cache_memory_bytes": "bad"}', overrides='{"max_model_len": "x"}')
    db.session.execute.return_value = SimpleNamespace(fetchone=lambda: text_row)
    assert db._node_selection(7, "org/m") == (None, None, None)

    db.session.execute.return_value = SimpleNamespace(fetchone=lambda: None)
    assert db._node_selection(7, "org/m") == (None, None, None)


def test_a_kv_cache_choice_stored_under_overrides_counts_with_capability_first():
    db = _db()
    row = SimpleNamespace(capability={}, overrides={"kv_cache_memory_bytes": "8G"})
    db.session.execute.return_value = SimpleNamespace(fetchone=lambda: row)
    assert db._node_selection(7, "org/m") == (8192.0, None, None)

    both = SimpleNamespace(capability={"kv_cache_memory_bytes": "2G"}, overrides={"kv_cache_memory_bytes": "8G"})
    db.session.execute.return_value = SimpleNamespace(fetchone=lambda: both)
    assert db._node_selection(7, "org/m") == (2048.0, None, None)


def test_a_choice_in_overrides_beyond_the_measured_range_skips_the_snapshot():
    db = _db()
    small = _candidate(1, min_kv_cache_mb=1024.0, max_kv_cache_mb=2048.0)
    selection = SimpleNamespace(capability={}, overrides={"kv_cache_memory_bytes": "8G"})
    db.session.execute.side_effect = _results([small], selection, [])

    assert db._pick_shared_snapshot(7, "org/m", _hint()) is None
