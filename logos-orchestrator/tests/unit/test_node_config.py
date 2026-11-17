"""Rough validation of a node's central model configuration."""

from __future__ import annotations

from logos.dbutils.dbrequest import NodeConfigEntry
from logos.node_config import conflict_warnings, entry_errors


def _entry(capability=None, overrides=None, model_name="org/model"):
    return NodeConfigEntry(model_name=model_name, capability=capability or {}, overrides=overrides or {})


def test_a_value_set_in_both_places_with_different_values_is_rejected():
    entry = _entry({"kv_cache_memory_bytes": "16G"}, {"kv_cache_memory_bytes": "8G"})
    assert any("differs between capability" in error for error in entry_errors([entry]))
    assert conflict_warnings([entry]) != []


def test_a_capability_only_key_in_overrides_is_rejected():
    assert entry_errors([_entry(overrides={"base_residency_mb": 5800})]) == [
        "org/model: 'base_residency_mb' belongs in capability, not overrides"
    ]


def test_the_same_value_in_both_places_and_engine_overrides_are_fine():
    entry = _entry({"tensor_parallel_size": 2}, {"tensor_parallel_size": 2, "quantization": "awq"})
    assert entry_errors([entry]) == []
    assert conflict_warnings([entry]) == []


def test_blank_repeated_and_unknown_entries_are_each_named():
    errors = entry_errors(
        [
            _entry(model_name=" "),
            _entry({"tensor_parallel_size": 1}),
            _entry({"gpu_devices": "0,1", "max_lanes": 2}, model_name="org/model "),
        ]
    )
    assert errors == [
        "model_name must not be blank",
        "org/model: listed twice",
        "org/model: capability key 'max_lanes' is not supported",
    ]


def test_capability_values_are_checked_against_their_expected_form():
    entry = _entry(
        {
            "tensor_parallel_size": True,
            "kv_cache_memory_bytes": "lots",
            "gpu_devices": "0;1",
            "base_residency_mb": 0,
            "min_context_fraction": 1.5,
        }
    )
    assert len(entry_errors([entry])) == 5


def test_error_items_split_model_key_message_and_suggestion():
    from logos.node_config import error_items

    items = error_items(
        [
            "org/a: max_modell_len: unknown setting (did you mean max_model_len?)",
            "org/b: max_model_len: Input should be a valid integer: unable to parse",
            "org/c: listed twice",
            "model_name must not be blank",
        ]
    )
    assert items[0] == {
        "model": "org/a",
        "key": "max_modell_len",
        "message": "unknown setting",
        "suggestion": "max_model_len",
    }
    assert items[1]["key"] == "max_model_len" and items[1]["message"].endswith("unable to parse")
    assert items[2] == {"model": "org/c", "key": None, "message": "listed twice", "suggestion": None}
    assert items[3] == {"model": None, "key": None, "message": "model_name must not be blank", "suggestion": None}


def test_kv_sizes_are_read_in_megabytes():
    from logos.node_config import kv_size_mb

    assert kv_size_mb("4G") == 4096.0
    assert kv_size_mb("512m") == 512.0
    assert kv_size_mb("2048K") == 2.0
    assert kv_size_mb("1048576") == 1.0
    assert kv_size_mb("lots") is None
    assert kv_size_mb("") is None
    assert kv_size_mb(None) is None


def test_a_calibration_covers_a_choice_inside_its_measured_range():
    from logos.node_config import selection_covered

    profile = {
        "min_kv_cache_mb": 1024.0,
        "max_kv_cache_mb": 8192.0,
        "kv_cache_to_max_model_len_pairs": [
            {"kv_mb": 1024.0, "max_model_len": 4096},
            {"kv_mb": 4096.0, "max_model_len": 16384},
            {"kv_mb": 8192.0, "max_model_len": 32768},
        ],
    }

    assert selection_covered(profile, None, None)
    assert selection_covered(profile, 4096.0, 16384)
    assert selection_covered(profile, 8192.0, 32768)
    assert not selection_covered(profile, 512.0, None)
    assert not selection_covered(profile, 16384.0, None)
    assert not selection_covered(profile, 4096.0, 20000)
    assert selection_covered({}, 99999.0, 999999)


def test_a_shared_calibration_carries_its_fingerprint_and_every_measured_size():
    from logos.model_profile_store import CALIBRATION_FIELDS, calibration_snapshot

    assert {"config_fingerprint", "calibrations_by_tp"} <= CALIBRATION_FIELDS
    profile = {
        "base_residency_mb": 1.0,
        "config_fingerprint": "FP",
        "calibrations_by_tp": [{"tp": 1}],
        "sync_revision": 3,
        "calibration_key_hash": "K",
    }

    assert calibration_snapshot(profile) == {
        "base_residency_mb": 1.0,
        "config_fingerprint": "FP",
        "calibrations_by_tp": [{"tp": 1}],
    }
