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
        ],
        ["org/a", "org/b", "org/c"],
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


def test_a_closing_suggestion_is_split_off_only_when_it_is_well_formed():
    from logos.node_config import _split_suggestion

    assert _split_suggestion("a: b: unknown setting (did you mean max_model_len?)  ") == (
        "a: b: unknown setting",
        "max_model_len",
    )
    assert _split_suggestion("no hint here") == ("no hint here", None)
    assert _split_suggestion("(did you mean ?)") == ("(did you mean ?)", None)
    assert _split_suggestion("(did you mean a?b?)") == ("(did you mean a?b?)", None)
    assert _split_suggestion("(did you mean x) and more") == ("(did you mean x) and more", None)
    assert _split_suggestion(" " * 5000 + "(did you mean x") == (" " * 5000 + "(did you mean x", None)


def test_error_items_take_only_a_submitted_name_for_the_model():
    from logos.node_config import error_items

    items = error_items(["Something broke: badly", "my: model: max_model_len: must be positive"], ["my: model"])
    assert items[0] == {"model": None, "key": None, "message": "Something broke: badly", "suggestion": None}
    assert items[1]["model"] == "my: model" and items[1]["key"] == "max_model_len"


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


def test_the_choice_is_read_from_either_mapping_with_capability_first():
    from logos.node_config import selection_of

    assert selection_of({"kv_cache_memory_bytes": "4G"}, {"max_model_len": 8192}) == (4096.0, 8192, None)
    assert selection_of({}, {"kv_cache_memory_bytes": "8G", "max_model_len": "4096"}) == (8192.0, 4096, None)
    assert selection_of({"kv_cache_memory_bytes": "2G"}, {"kv_cache_memory_bytes": "8G"}) == (2048.0, None, None)
    assert selection_of({"max_model_len": 16384}, {"max_model_len": 4096}) == (None, 16384, None)
    assert selection_of({"tensor_parallel_size": 2}, {"tensor_parallel_size": 4}) == (None, None, 2)
    assert selection_of({}, {"tensor_parallel_size": "4"}) == (None, None, 4)
    assert selection_of(None, None) == (None, None, None)
    assert selection_of({}, {"max_model_len": True, "kv_cache_memory_bytes": "lots", "tensor_parallel_size": 0}) == (
        None,
        None,
        None,
    )


def test_a_tensor_parallel_size_is_covered_only_when_it_was_measured():
    from logos.node_config import selection_covered

    profile = {
        "tensor_parallel_size": 1,
        "min_kv_cache_mb": 1024.0,
        "max_kv_cache_mb": 4096.0,
        "calibrations_by_tp": [
            {
                "tp": 1,
                "min_kv_cache_mb": 1024.0,
                "max_kv_cache_mb": 4096.0,
                "kv_cache_to_max_model_len_pairs": [{"kv_mb": 4096.0, "max_model_len": 8192}],
            },
            {
                "tp": 2,
                "min_kv_cache_mb": 2048.0,
                "max_kv_cache_mb": 16384.0,
                "kv_cache_to_max_model_len_pairs": [
                    {"kv_mb": 2048.0, "max_model_len": 8192},
                    {"kv_mb": 16384.0, "max_model_len": 32768},
                ],
            },
        ],
    }

    assert selection_covered(profile, None, None, 1)
    assert selection_covered(profile, None, None, 2)
    assert not selection_covered(profile, None, None, 4)
    # The range is the one of the chosen size: 12 GB is out of reach at TP 1 and fine at TP 2.
    assert not selection_covered(profile, 12288.0, None, 1)
    assert selection_covered(profile, 12288.0, 8192, 2)
    assert not selection_covered(profile, 12288.0, 32768, 2)


def test_an_older_calibration_knows_the_one_size_its_lane_uses():
    from logos.node_config import selection_covered

    older = {"tensor_parallel_size": 2, "min_kv_cache_mb": 1024.0, "max_kv_cache_mb": 4096.0}

    assert selection_covered(older, 2048.0, None, 2)
    assert not selection_covered(older, 2048.0, None, 1)
    # Nothing known about the size: nothing counts against the choice.
    assert selection_covered({"min_kv_cache_mb": 1024.0, "max_kv_cache_mb": 4096.0}, 2048.0, None, 4)


def test_a_sweep_that_starts_above_the_chosen_cache_does_not_cover_a_context():
    from logos.node_config import selection_covered

    profile = {"kv_cache_to_max_model_len_pairs": [{"kv_mb": 4096.0, "max_model_len": 16384}]}

    assert not selection_covered(profile, 2048.0, 4096)
    assert selection_covered(profile, 2048.0, None)
    assert selection_covered({"kv_cache_to_max_model_len_pairs": [{"kv_mb": 1.0, "max_model_len": 0}]}, 1.0, 4096)


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
