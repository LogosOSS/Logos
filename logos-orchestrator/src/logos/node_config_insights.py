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


def calibration_view(profile: Mapping[str, Any] | None, calibrated_at: Any = None) -> dict[str, Any]:
    """Calibration status and the measured values of one model on this node."""
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
    pairs = [
        {"kv_mb": p.get("kv_mb"), "max_model_len": p.get("max_model_len"), "measured": True}
        for p in profile.get("kv_cache_to_max_model_len_pairs") or []
        if isinstance(p, dict)
    ]
    result = {
        "tp": profile.get("tensor_parallel_size") or 1,
        "active": True,
        "base_residency_mb": profile.get("base_residency_mb"),
        "loaded_vram_mb": profile.get("loaded_vram_mb"),
        "kv_min_mb": profile.get("min_kv_cache_mb"),
        "kv_max_mb": profile.get("max_kv_cache_mb"),
        "pairs": pairs,
        "cold_load_time_s": profile.get("cold_load_time_s"),
        "wake_from_sleep_time_s": profile.get("wake_from_sleep_time_s"),
        "enforce_eager": profile.get("enforce_eager_at_calibration"),
    }
    return {
        "status": status,
        "reason": reason,
        "calibrated_at": calibrated_at.isoformat() if hasattr(calibrated_at, "isoformat") else calibrated_at,
        "stale": status == "outdated",
        "context_limit": profile.get("max_context_length"),
        "results": [result],
    }


def benchmark_view(summary: Mapping[str, Any] | None, runs: Mapping[int, Mapping[str, Any]], current: Mapping[str, Any]):
    """The best benchmark results of a model on this node; None without one."""
    if not summary or not summary.get("best_observed"):
        return None

    def _best(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        run = runs.get(row["benchmark_id"], {})
        return {
            "benchmark_id": row["benchmark_id"],
            "throughput": row["throughput"],
            "settings": run.get("serving") or {},
            "recorded_at": run.get("recorded_at"),
        }

    eligible = _best(summary.get("best_eligible"))
    return {
        "best_eligible": eligible,
        "best_observed": _best(summary.get("best_observed")),
        "current": dict(current),
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
) -> dict[str, Any]:
    """The whole answer of ``insights`` for one node."""
    entries = {entry["model_name"]: entry for entry in config["entries"]}
    reported = set((snapshot or {}).get("capabilities_models") or []) | set(
        (snapshot or {}).get("configured_models") or []
    )
    names = [entry["model_name"] for entry in config["entries"]]
    names += sorted((reported | set(profiles)) - set(names))
    models = []
    for name in names:
        entry = entries.get(name)
        models.append(
            {
                "model_name": name,
                "reported": name in reported,
                "row": entry is not None,
                "source": sources.get(name),
                "configured": is_configured(entry),
                "calibration": calibration_view(profiles.get(name), calibrated_at.get(name)),
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
