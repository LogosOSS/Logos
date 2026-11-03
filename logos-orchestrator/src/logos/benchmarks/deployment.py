"""Public serving configuration from current worker telemetry."""

from logos.benchmarks.guidellm_runner import extract_serving_configuration


def deployment_snapshot(snapshot, model, *, online, series_key=None, maintenance=False):
    """Do not label an offline, ambiguous or temporary benchmark lane as production."""
    lanes = [lane for lane in ((snapshot or {}).get("runtime") or {}).get("lanes", []) if lane.get("model") == model]
    lane = lanes[0] if len(lanes) == 1 else None
    return {
        "available": bool(online and lane),
        "observed_at": (snapshot or {}).get("last_heartbeat"),
        "lane_state": lane.get("runtime_state") if lane else None,
        "maintenance": maintenance,
        "series_key": series_key,
        "configuration": extract_serving_configuration(snapshot, model) if online and lane else {},
    }
