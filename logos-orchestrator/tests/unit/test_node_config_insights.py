"""What the model editor shows next to the settings of a node."""

from __future__ import annotations

import datetime

from logos.node_config_insights import benchmark_view, build_insights, calibration_view, gpu_view, is_configured

_PROFILE = {
    "residency_source": "calibrated",
    "tensor_parallel_size": 2,
    "base_residency_mb": 41200.0,
    "loaded_vram_mb": 40000.0,
    "min_kv_cache_mb": 1024.0,
    "max_kv_cache_mb": 4096.0,
    "max_context_length": 32768,
    "cold_load_time_s": 54.6,
    "wake_from_sleep_time_s": 1.3,
    "enforce_eager_at_calibration": True,
    "kv_cache_to_max_model_len_pairs": [{"kv_mb": 1024.0, "max_model_len": 5440}, "junk"],
}


def test_a_model_with_tp_kv_and_context_is_configured_wherever_they_are_set():
    entry = {
        "capability": {"tensor_parallel_size": 2, "kv_cache_memory_bytes": "4G"},
        "overrides": {"max_model_len": 8192},
    }
    assert is_configured(entry) is True


def test_a_model_without_a_placement_or_a_row_is_not_configured():
    assert is_configured(None) is False
    assert is_configured({"capability": {}, "overrides": {"quantization": "awq"}}) is False
    assert (
        is_configured({"capability": {"tensor_parallel_size": 2, "kv_cache_memory_bytes": ""}, "overrides": {}})
        is False
    )


def test_a_calibrated_profile_shows_its_measured_values():
    when = datetime.datetime(2026, 11, 1, 5, 0)
    view = calibration_view(_PROFILE, when)
    assert view["status"] == "calibrated" and view["stale"] is False
    assert view["calibrated_at"] == when.isoformat() and view["context_limit"] == 32768
    (result,) = view["results"]
    assert result["tp"] == 2 and result["active"] is True and result["kv_max_mb"] == 4096.0
    assert result["pairs"] == [{"kv_mb": 1024.0, "max_model_len": 5440, "measured": True}]


def test_a_stale_calibration_is_outdated_and_an_unsupported_one_names_its_reason():
    assert calibration_view({**_PROFILE, "calibration_stale": True})["status"] == "outdated"
    view = calibration_view({**_PROFILE, "calibration_unsupported": True, "calibration_unsupported_reason": "too-big"})
    assert (view["status"], view["reason"]) == ("unsupported", "too-big")


def test_a_model_without_a_measurement_is_not_calibrated():
    assert calibration_view(None) == {"status": "not_calibrated", "reason": None, "results": []}
    assert calibration_view({"residency_source": "hf"})["status"] == "not_calibrated"
    view = calibration_view(
        {"residency_source": "hf", "calibration_unsupported": True, "calibration_unsupported_reason": "x"}
    )
    assert (view["status"], view["reason"]) == ("unsupported", "x")


def test_the_benchmark_view_names_the_best_runs_and_the_current_settings():
    summary = {
        "best_observed": {"benchmark_id": 3, "throughput": 60.0},
        "best_eligible": {"benchmark_id": 2, "throughput": 52.3},
    }
    runs = {
        2: {"serving": {"tensor_parallel_size": 4}, "recorded_at": "t2", "ttft_s": 0.47, "latency_s": 5.5},
        3: {"serving": {}, "recorded_at": "t3"},
    }
    view = benchmark_view(summary, runs, {"tensor_parallel_size": 2})
    assert view["best_eligible"] == {
        "benchmark_id": 2,
        "throughput": 52.3,
        "settings": {"tensor_parallel_size": 4},
        "recorded_at": "t2",
        "ttft_s": 0.47,
        "latency_s": 5.5,
        # No baseline was recorded for this run, so there is nothing to compare its throughput with.
        "baseline_throughput": None,
    }
    assert view["best_observed"]["ttft_s"] is None
    assert view["best_observed"]["benchmark_id"] == 3 and view["eligible_for_comparison"] is True
    assert view["current"] == {"tensor_parallel_size": 2}


def test_the_benchmark_view_gives_the_recorded_cache_size_the_key_of_the_configuration():
    summary = {"best_observed": {"benchmark_id": 3, "throughput": 60.0}, "best_eligible": None}
    runs = {3: {"serving": {"tensor_parallel_size": 2, "kv_cache_memory": "8G", "max_model_len": 4096}}}

    view = benchmark_view(summary, runs, {"kv_cache_memory": "4G"})

    assert view["best_observed"]["settings"] == {
        "tensor_parallel_size": 2,
        "kv_cache_memory_bytes": "8G",
        "max_model_len": 4096,
    }
    assert view["current"] == {"kv_cache_memory_bytes": "4G"}


def test_the_best_eligible_run_carries_the_throughput_of_the_run_it_was_compared_with():
    summary = {
        "best_observed": {"benchmark_id": 4, "throughput": 52.3, "baseline_id": 3},
        "best_eligible": {"benchmark_id": 4, "throughput": 52.3, "baseline_id": 3},
        "comparisons": [
            {"benchmark_id": 4, "baseline_id": 3, "throughput": 52.3},
            {"benchmark_id": 3, "baseline_id": None, "throughput": 48.4},
        ],
    }

    view = benchmark_view(summary, {}, {})

    assert view["best_eligible"]["baseline_throughput"] == 48.4


def test_a_placed_model_carries_its_placement_and_an_automatic_one_none():
    placed = {
        "model_name": "org/a",
        "capability": {"tensor_parallel_size": 2, "kv_cache_memory_bytes": "4G"},
        "overrides": {"max_model_len": 8192, "quantization": "awq"},
    }
    automatic = {"model_name": "org/b", "capability": {}, "overrides": {"quantization": "awq"}}

    result = build_insights(
        provider_id=1,
        config={"revision": 1, "entries": [placed, automatic]},
        sources={},
        profiles={},
        calibrated_at={},
        snapshot=None,
        supports={},
        calibrating=False,
        benchmarks={},
    )

    first, second = result["models"]
    assert first["placement"] == {"tensor_parallel_size": 2, "kv_cache_memory_bytes": "4G", "max_model_len": 8192}
    assert first["configured"] is True
    assert second["placement"] is None and second["configured"] is False


def test_a_model_in_a_lane_carries_what_it_runs_with():
    lane = {
        "model": "org/a",
        "runtime_state": "running",
        "lane_config": {
            "vllm_config": {"tensor_parallel_size": 1, "kv_cache_memory_bytes": "3G", "max_model_len": 16368}
        },
    }
    cold = {"model": "org/b", "runtime_state": "cold", "lane_config": {"vllm_config": {"tensor_parallel_size": 2}}}
    snapshot = {"capabilities_models": ["org/a", "org/b"], "configured_models": [], "runtime": {"lanes": [lane, cold]}}

    result = build_insights(
        provider_id=1,
        config={"revision": 1, "entries": []},
        sources={},
        profiles={},
        calibrated_at={},
        snapshot=snapshot,
        supports={},
        calibrating=False,
        benchmarks={},
    )

    first, second = result["models"]
    assert first["running"] == {"tensor_parallel_size": 1, "kv_cache_memory_bytes": "3G", "max_model_len": 16368}
    assert second["running"] is None


def test_each_model_carries_its_catalog_id_when_it_has_one():
    config = {"revision": 1, "entries": [{"model_name": "org/a", "capability": {}, "overrides": {}}]}

    result = build_insights(
        provider_id=1,
        config=config,
        sources={},
        profiles={"org/b": {"residency_source": "calibrated", "tensor_parallel_size": 1, "base_residency_mb": 9.0}},
        calibrated_at={},
        snapshot=None,
        supports={},
        calibrating=False,
        benchmarks={},
        model_ids={"org/a": 7},
    )

    assert {m["model_name"]: m["model_id"] for m in result["models"]} == {"org/a": 7, "org/b": None}


def test_a_stored_profile_without_a_calibration_of_a_model_the_node_no_longer_has_is_not_listed():
    config = {"revision": 1, "entries": [{"model_name": "org/a", "capability": {}, "overrides": {}}]}
    snapshot = {"capabilities_models": ["org/a"], "configured_models": [], "runtime": {}}

    result = build_insights(
        provider_id=1,
        config=config,
        sources={},
        profiles={"org/old": {"loaded_vram_mb": 9000.0}, "org/empty": {}},
        calibrated_at={},
        snapshot=snapshot,
        supports={},
        calibrating=False,
        benchmarks={},
    )

    assert [m["model_name"] for m in result["models"]] == ["org/a"]


def test_no_eligible_run_is_not_comparable_and_no_run_at_all_is_none():
    only_observed = {"best_observed": {"benchmark_id": 3, "throughput": 1.0}, "best_eligible": None}
    assert benchmark_view(only_observed, {}, {})["eligible_for_comparison"] is False
    assert benchmark_view({"best_observed": None, "best_eligible": None}, {}, {}) is None
    assert benchmark_view(None, {}, {}) is None


def test_gpus_come_from_the_workers_device_summary():
    runtime = {
        "devices": {
            "devices": [{"name": "A", "memory_total_mb": 24000, "memory_used_mb": 10, "extra": {"index": 1}}, "junk"]
        }
    }
    assert gpu_view(runtime) == [{"index": 1, "name": "A", "total_mb": 24000, "used_mb": 10}]
    assert gpu_view(None) == []


def _insights(snapshot, config=None, **kwargs):
    entries = [{"model_name": "org/a", "capability": {}, "overrides": {"quantization": "awq"}}]
    return build_insights(
        provider_id=4,
        config=config or {"revision": 3, "yaml_imported": True, "entries": entries},
        sources={"org/a": "yaml_import"},
        profiles=kwargs.get("profiles", {"org/a": _PROFILE, "org/stray": _PROFILE}),
        calibrated_at={},
        snapshot=snapshot,
        supports={"sync_node_config": True},
        calibrating=kwargs.get("calibrating", False),
        benchmarks={},
    )


def test_the_answer_lists_stored_models_first_and_adds_reported_and_profiled_ones():
    snapshot = {"capabilities_models": ["org/a", "org/b"], "configured_models": [], "runtime": {}}
    result = _insights(snapshot)
    assert [m["model_name"] for m in result["models"]] == ["org/a", "org/b", "org/stray"]
    first, second, third = result["models"]
    assert (first["row"], first["reported"], first["source"], first["configured"]) == (True, True, "yaml_import", False)
    assert (second["row"], second["reported"], second["calibration"]["status"]) == (False, True, "not_calibrated")
    assert (third["row"], third["reported"], third["calibration"]["status"]) == (False, False, "calibrated")
    assert result["worker"] == {"online": True, "supports_actions": {"sync_node_config": True}, "config_revision": 3}


def test_an_offline_worker_is_reported_and_the_calibration_flag_follows_the_provider():
    result = _insights(None, calibrating=True, profiles={})
    assert result["worker"]["online"] is False and result["gpus"] == []
    assert [m["calibrating"] for m in result["models"]] == [True]


def test_every_measured_tensor_parallel_size_is_listed_and_the_lanes_one_is_active():
    profile = {
        "residency_source": "calibrated",
        "tensor_parallel_size": 2,
        "calibrations_by_tp": [
            {"tp": 4, "base_residency_mb": 250.0, "min_kv_cache_mb": 512.0, "max_kv_cache_mb": 2048.0},
            {
                "tp": 2,
                "base_residency_mb": 500.0,
                "min_kv_cache_mb": 1024.0,
                "max_kv_cache_mb": 4096.0,
                "kv_cache_to_max_model_len_pairs": [{"kv_mb": 1024.0, "max_model_len": 4096}, "junk"],
                "cold_load_time_s": 40.0,
                "enforce_eager": True,
            },
            {"tp": "x"},
            "junk",
        ],
    }

    results = calibration_view(profile)["results"]

    assert [(r["tp"], r["active"]) for r in results] == [(2, True), (4, False)]
    assert results[0]["pairs"] == [{"kv_mb": 1024.0, "max_model_len": 4096, "measured": True}]
    assert (results[0]["kv_min_mb"], results[0]["kv_max_mb"], results[0]["cold_load_time_s"]) == (1024.0, 4096.0, 40.0)
    assert results[0]["enforce_eager"] is True


def test_each_size_names_the_cache_its_total_was_measured_with():
    profile = {
        "residency_source": "calibrated",
        "tensor_parallel_size": 2,
        "calibrated_kv_mb": 4096.0,
        "calibrations_by_tp": [
            {"tp": 1, "base_residency_mb": 16000.0, "calibrated_kv_mb": 8192.0},
            {"tp": 2, "base_residency_mb": 9000.0, "calibrated_kv_mb": 4096.0},
        ],
    }

    assert [r["measured_kv_mb"] for r in calibration_view(profile)["results"]] == [8192.0, 4096.0]

    older = {"residency_source": "calibrated", "tensor_parallel_size": 1, "base_residency_mb": 5.0}
    assert calibration_view(older)["results"][0]["measured_kv_mb"] is None


def test_sizes_the_node_has_no_gpus_for_are_not_offered():
    profile = {
        "residency_source": "calibrated",
        "tensor_parallel_size": 1,
        "calibrations_by_tp": [{"tp": 1}, {"tp": 2}, {"tp": 4}],
    }

    assert [r["tp"] for r in calibration_view(profile, gpu_slice=2)["results"]] == [1, 2]
    assert [r["tp"] for r in calibration_view(profile)["results"]] == [1, 2, 4]
    # The size the lane uses stays, whatever the slice says.
    assert [r["tp"] for r in calibration_view(profile, gpu_slice=0)["results"]] == [1, 2, 4]


def test_a_node_with_two_gpus_does_not_get_the_sizes_of_a_four_gpu_snapshot():
    config = {"revision": 1, "entries": []}
    profile = {
        "residency_source": "calibrated",
        "tensor_parallel_size": 1,
        "calibrations_by_tp": [{"tp": 1}, {"tp": 2}, {"tp": 4}],
    }

    result = build_insights(
        provider_id=1,
        config=config,
        sources={},
        profiles={"org/a": profile},
        calibrated_at={},
        snapshot=None,
        supports={},
        calibrating=False,
        benchmarks={},
        gpu_slices={"org/a": 2},
    )

    assert [r["tp"] for r in result["models"][0]["calibration"]["results"]] == [1, 2]


def test_a_profile_without_the_list_still_shows_the_size_it_has():
    profile = {"residency_source": "calibrated", "tensor_parallel_size": 4, "base_residency_mb": 9.0}

    results = calibration_view(profile)["results"]

    assert [(r["tp"], r["active"], r["base_residency_mb"]) for r in results] == [(4, True, 9.0)]
    assert (
        calibration_view({"residency_source": "calibrated", "calibrations_by_tp": [{"tp": "x"}]})["results"][0]["tp"]
        == 1
    )
