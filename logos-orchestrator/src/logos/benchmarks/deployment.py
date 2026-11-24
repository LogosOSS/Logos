"""Public serving configuration from current worker telemetry."""

from logos.benchmarks.guidellm_runner import lane_serving_configuration


def deployment_snapshot(snapshot, model, *, online, series_key=None, maintenance=False):
    """Report serving replicas separately from the availability of comparable settings."""
    snapshot = snapshot or {}
    runtime = snapshot.get("runtime") or {}
    lanes = [lane for lane in runtime.get("lanes", []) if lane.get("model") == model]
    ready = [
        lane
        for lane in lanes
        if online and lane.get("runtime_state") in {"loaded", "running"} and lane.get("sleep_state") != "sleeping"
    ]
    observed_lanes = (ready or lanes) if online else []
    configurations = [lane_serving_configuration(lane) for lane in observed_lanes]
    configuration = configurations[0] if configurations else {}
    mixed = any(item != configuration for item in configurations)
    states = {lane.get("runtime_state") for lane in lanes}
    lane_state = next(iter(states)) if len(states) == 1 else None
    if not online:
        status = "offline"
    elif ready:
        status = "active"
    elif not runtime or snapshot.get("first_status_received") is False:
        status = "waiting"
    elif not lanes:
        status = "configured" if model in snapshot.get("configured_models", []) else "no_lane"
    elif all(lane.get("sleep_state") == "sleeping" or lane.get("runtime_state") == "sleeping" for lane in lanes):
        status = "sleeping"
    else:
        status = lane_state if lane_state in {"starting", "stopped", "error", "cold"} else "not_ready"
    return {
        "available": bool(ready),
        "status": status,
        "active_replicas": len(ready),
        "mixed_configuration": mixed,
        "observed_at": snapshot.get("last_heartbeat"),
        "lane_state": ready[0].get("runtime_state") if ready else lane_state,
        "maintenance": maintenance,
        "series_key": series_key if ready and not mixed else None,
        "configuration": configuration if ready and not mixed else {},
        "configured_settings": configuration if not ready and not mixed else {},
    }
