"""What the editor of a node's models shows next to the settings.

Pure functions over data the endpoint gathered: stored configuration, central
profiles, the live worker and benchmark results. Nothing here writes.
"""

from __future__ import annotations

from typing import Any, Mapping

REQUIRED_PLACEMENT = ("tensor_parallel_size", "kv_cache_memory_bytes", "max_model_len")


def is_configured(entry: Mapping[str, Any] | None) -> bool:
    """A configuration that places the model: TP, KV cache and context set."""
    if entry is None:
        return False
    merged = {**(entry.get("overrides") or {}), **(entry.get("capability") or {})}
    return all(merged.get(key) not in (None, "") for key in REQUIRED_PLACEMENT)


def placement_of(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """TP, KV cache and context that place the model, or None while the worker places it itself."""
    if not is_configured(entry):
        return None
    merged = {**(entry.get("overrides") or {}), **(entry.get("capability") or {})}
    return {key: merged[key] for key in REQUIRED_PLACEMENT}


def running_placement(snapshot: Mapping[str, Any] | None, model: str) -> dict[str, Any] | None:
    """TP, KV cache and context a lane of the model runs with now, or None while no lane holds the model.

    For a model without a placement of its own, these are the values the planner of the worker chose.
    """
    lanes = (((snapshot or {}).get("runtime") or {}).get("lanes")) or []
    for lane in lanes:
        if not isinstance(lane, dict) or lane.get("model") != model:
            continue
        if lane.get("runtime_state") not in ("loaded", "running", "sleeping"):
            continue
        vllm = ((lane.get("lane_config") or {}).get("vllm_config")) or {}
        found = {key: vllm[key] for key in REQUIRED_PLACEMENT if vllm.get(key) not in (None, "")}
        if found:
            return found
    return None


def _pairs(raw: Any) -> list[dict[str, Any]]:
    return [
        {"kv_mb": p.get("kv_mb"), "max_model_len": p.get("max_model_len"), "measured": True}
        for p in raw or []
        if isinstance(p, dict)
    ]


def _active_result(profile: Mapping[str, Any], tp: Any) -> dict[str, Any]:
    """The size the lane uses, from the profile's own fields."""
    return {
        "tp": tp,
        "active": True,
        "base_residency_mb": profile.get("base_residency_mb"),
        "loaded_vram_mb": profile.get("loaded_vram_mb"),
        "measured_kv_mb": profile.get("calibrated_kv_mb"),
        "kv_min_mb": profile.get("min_kv_cache_mb"),
        "kv_max_mb": profile.get("max_kv_cache_mb"),
        "pairs": _pairs(profile.get("kv_cache_to_max_model_len_pairs")),
        "cold_load_time_s": profile.get("cold_load_time_s"),
        "wake_from_sleep_time_s": profile.get("wake_from_sleep_time_s"),
        "enforce_eager": profile.get("enforce_eager_at_calibration"),
    }


def _results_by_tp(raw: Any, active_tp: Any) -> list[dict[str, Any]]:
    """Every tensor-parallel size the calibration measured, smallest first."""
    results = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("tp"), int):
            continue
        results.append(
            {
                "tp": item["tp"],
                "active": item["tp"] == active_tp,
                "base_residency_mb": item.get("base_residency_mb"),
                "loaded_vram_mb": item.get("loaded_vram_mb"),
                "measured_kv_mb": item.get("calibrated_kv_mb"),
                "kv_min_mb": item.get("min_kv_cache_mb"),
                "kv_max_mb": item.get("max_kv_cache_mb"),
                "pairs": _pairs(item.get("kv_cache_to_max_model_len_pairs")),
                "cold_load_time_s": item.get("cold_load_time_s"),
                "wake_from_sleep_time_s": item.get("wake_from_sleep_time_s"),
                "enforce_eager": item.get("enforce_eager"),
            }
        )
    return sorted(results, key=lambda r: r["tp"])


def calibration_view(
    profile: Mapping[str, Any] | None, calibrated_at: Any = None, gpu_slice: int | None = None
) -> dict[str, Any]:
    """Calibration status and the measured values of one model on this node.

    ``gpu_slice`` is the number of GPUs the node can serve the model on. A shared calibration
    may carry results for larger tensor-parallel sizes, which this node cannot run.
    """
    if profile is None or profile.get("residency_source") not in ("calibrated", "measured"):
        view: dict[str, Any] = {"status": "not_calibrated", "reason": None, "results": []}
        if profile and profile.get("calibration_unsupported"):
            view.update(status="unsupported", reason=profile.get("calibration_unsupported_reason"))
        return view
    if profile.get("calibration_unsupported"):
        status, reason = "unsupported", profile.get("calibration_unsupported_reason")
    elif profile.get("calibration_stale"):
        status, reason = "outdated", None
    else:
        status, reason = "calibrated", None
    active_tp = profile.get("tensor_parallel_size") or 1
    results = _results_by_tp(profile.get("calibrations_by_tp"), active_tp)
    if gpu_slice:
        results = [result for result in results if result["tp"] <= gpu_slice or result["active"]]
    results = results or [_active_result(profile, active_tp)]
    return {
        "status": status,
        "reason": reason,
        "calibrated_at": calibrated_at.isoformat() if hasattr(calibrated_at, "isoformat") else calibrated_at,
        "stale": status == "outdated",
        "context_limit": profile.get("max_context_length"),
        "results": results,
    }


def _configuration_keys(serving: Mapping[str, Any] | None) -> dict[str, Any]:
    """Serving settings of a run under the keys of the node configuration.

    A run records the cache size as ``kv_cache_memory``; the editor reads it as ``kv_cache_memory_bytes``.
    """
    settings = dict(serving or {})
    if "kv_cache_memory" in settings:
        settings.setdefault("kv_cache_memory_bytes", settings.pop("kv_cache_memory"))
    return settings


def benchmark_view(
    summary: Mapping[str, Any] | None, runs: Mapping[int, Mapping[str, Any]], current: Mapping[str, Any]
):
    """The best benchmark results of a model on this node; None without one."""
    if not summary or not summary.get("best_observed"):
        return None

    def _best(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        run = runs.get(row["benchmark_id"], {})
        settings = _configuration_keys(run.get("serving"))
        return {
            "benchmark_id": row["benchmark_id"],
            "throughput": row["throughput"],
            "settings": settings,
            "recorded_at": run.get("recorded_at"),
            "ttft_s": run.get("ttft_s"),
            "latency_s": run.get("latency_s"),
        }

    eligible = _best(summary.get("best_eligible"))
    if eligible is not None:
        # The run it was compared with: what the model ran with before.
        baseline_id = (summary.get("best_eligible") or {}).get("baseline_id")
        eligible["baseline_throughput"] = next(
            (
                row.get("throughput")
                for row in summary.get("comparisons") or []
                if baseline_id is not None and row.get("benchmark_id") == baseline_id
            ),
            None,
        )
    return {
        "best_eligible": eligible,
        "best_observed": _best(summary.get("best_observed")),
        "current": _configuration_keys(current),
        "eligible_for_comparison": eligible is not None,
    }


def gpu_view(runtime: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    devices = ((runtime or {}).get("devices") or {}).get("devices") or []
    return [
        {
            "index": (device.get("extra") or {}).get("index", position),
            "name": device.get("name"),
            "total_mb": device.get("memory_total_mb"),
            "used_mb": device.get("memory_used_mb"),
        }
        for position, device in enumerate(devices)
        if isinstance(device, dict)
    ]


def build_insights(
    *,
    provider_id: int,
    config: Mapping[str, Any],
    sources: Mapping[str, str],
    profiles: Mapping[str, Mapping[str, Any]],
    calibrated_at: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    supports: Mapping[str, bool],
    calibrating: bool,
    benchmarks: Mapping[str, Any],
    model_ids: Mapping[str, int] | None = None,
    gpu_slices: Mapping[str, int | None] | None = None,
) -> dict[str, Any]:
    """The whole answer of ``insights`` for one node."""
    entries = {entry["model_name"]: entry for entry in config["entries"]}
    reported = set((snapshot or {}).get("capabilities_models") or []) | set(
        (snapshot or {}).get("configured_models") or []
    )
    names = [entry["model_name"] for entry in config["entries"]]
    # A stored profile of a model that the node neither reports nor has a configuration for is listed only
    # while it holds a calibration. Without one, it is a remnant of a model the node had once.
    holds_calibration = {
        name
        for name, profile in profiles.items()
        if calibration_view(profile, calibrated_at.get(name), (gpu_slices or {}).get(name))["status"]
        != "not_calibrated"
    }
    names += sorted((reported | holds_calibration) - set(names))
    models = []
    for name in names:
        entry = entries.get(name)
        models.append(
            {
                "model_name": name,
                "model_id": (model_ids or {}).get(name),
                "reported": name in reported,
                "row": entry is not None,
                "source": sources.get(name),
                "configured": is_configured(entry),
                "placement": placement_of(entry),
                "running": running_placement(snapshot, name),
                "calibration": calibration_view(
                    profiles.get(name), calibrated_at.get(name), (gpu_slices or {}).get(name)
                ),
                "benchmark": benchmarks.get(name),
                "calibrating": calibrating,
            }
        )
    return {
        "provider_id": provider_id,
        "worker": {
            "online": snapshot is not None,
            "supports_actions": dict(supports),
            "config_revision": config["revision"],
        },
        "gpus": gpu_view((snapshot or {}).get("runtime")),
        "models": models,
    }
