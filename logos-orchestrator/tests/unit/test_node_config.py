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
