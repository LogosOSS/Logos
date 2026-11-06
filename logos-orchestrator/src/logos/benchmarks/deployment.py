"""Public serving configuration from current worker telemetry."""

from logos.benchmarks.guidellm_runner import extract_serving_configuration


def deployment_snapshot(snapshot, model, *, online, series_key=None, maintenance=False):
    """Expose serving settings only for an online, unambiguous, ready lane."""
    lanes = [lane for lane in ((snapshot or {}).get("runtime") or {}).get("lanes", []) if lane.get("model") == model]
    lane = lanes[0] if len(lanes) == 1 else None
    lane_state = lane.get("runtime_state") if lane else None
    available = bool(online and lane and lane_state in {"loaded", "running"} and lane.get("sleep_state") != "sleeping")
    return {
        "available": available,
        "observed_at": (snapshot or {}).get("last_heartbeat"),
        "lane_state": lane_state,
        "maintenance": maintenance,
        "series_key": series_key,
        "configuration": extract_serving_configuration(snapshot, model) if available else {},
    }
