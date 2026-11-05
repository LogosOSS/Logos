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
