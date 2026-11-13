"""Normalize fresh engine observations for the runner's capacity decisions.

The available count is the current dispatch step, not a physical maximum.
Only an engine wait list establishes saturation; running above the lane's
full-context guarantee does not.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Snapshot:
    payload: dict
    available_slots: int
    sample_id: tuple[tuple[str, str, str], ...]
    observation_id: tuple[tuple[str, str, str], ...]
    known: bool


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if math.isfinite(value) and value >= 0 else None


def _fresh(runtime: dict, now: datetime, max_age_s: float) -> bool:
    observed = runtime.get("observed_at")
    if not isinstance(observed, str) or not runtime.get("session_id"):
        return False
    try:
        moment = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return False
        age = (now - moment).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return False
    return -max_age_s <= age <= max_age_s


def prepare(payload: dict, lane, *, max_age_s: float, now: datetime | None = None) -> Snapshot | None:
    """Use current engine lanes while retaining matching ownership metadata.

    ``None`` selects the legacy parser during rolling upgrades. Once a new
    payload is present, missing or stale observations never become idle
    capacity. Fresh compatible peers can still serve a model whose other
    provider is unavailable.
    """
    providers = ((payload.get("logosnode") or {}).get("providers")) or {}
    wanted = set(lane or ())
    models = [
        (str(pid), str(mid), provider, model)
        for pid, provider in providers.items()
        if isinstance(provider, dict)
        for mid, model in (provider.get("models") or {}).items()
        if isinstance(model, dict)
    ]
    modern_runtime = any(
        isinstance(provider, dict)
        and isinstance(provider.get("runtime"), dict)
        and "observed_at" in provider["runtime"]
        for provider in providers.values()
    )
    if not modern_runtime and not any("engine_lanes" in model for _, _, _, model in models):
        return None
    selected = [
        (pid, mid, provider, model) for pid, mid, provider, model in models if lane is None or (pid, mid) in wanted
    ]

    now = now or datetime.now(timezone.utc)
    result = {"logosnode": {"providers": {}}}
    headroom: dict[str, int] = {}
    observed_models: set[str] = set()
    required_models: set[str] = set()
    markers: set[tuple[str, str, str]] = set()
    observations: set[tuple[str, str, str]] = set()
    clear_models: set[str] = set()
    for pid, mid, provider, model in selected:
        name = str(model.get("model_name") or mid).strip().lower()
        if model.get("loaded"):
            required_models.add(name)
        # Queue depth and its ownership are scheduler state, not engine
        # telemetry. Preserve them even when the provider cannot serve.
        normalized = deepcopy(model)
        normalized.update(loaded=False, lane_parallel=0, max_capacity=0)
        signals = normalized.get("scheduler_signals")
        if not isinstance(signals, dict):
            signals = {}
        signals.update(requests_running_current=0, queue_waiting_current=0)
        normalized["scheduler_signals"] = signals
        result["logosnode"]["providers"].setdefault(pid, {"models": {}})["models"][mid] = normalized
        runtime = provider.get("runtime") or {}
        if (
            not isinstance(runtime, dict)
            or not runtime.get("online")
            or runtime.get("cordoned")
            or not _fresh(runtime, now, max_age_s)
        ):
            continue
        lanes = model.get("engine_lanes")
        if not isinstance(lanes, list):
            continue
        ready = [
            item for item in lanes if isinstance(item, dict) and item.get("runtime_state") in ("loaded", "running")
        ]
        usable = [
            item
            for item in ready
            if _count(item.get("requests_running")) is not None and _count(item.get("queue_waiting")) is not None
        ]
        if ready and not usable:
            continue
        running = sum(_count(item["requests_running"]) for item in usable)
        waiting = sum(_count(item["queue_waiting"]) for item in usable)
        clear = any(_count(item["queue_waiting"]) == 0 for item in usable)
        admission = model.get("admission") or {}
        available = 0
        if clear and isinstance(admission, dict) and admission.get("can_admit") is True:
            reported = _count(admission.get("batch_limit"))
            available = reported if reported is not None else 1
        if usable:
            observed_models.add(name)
            headroom[name] = headroom.get(name, 0) + available
            marker = (pid, str(runtime["session_id"]), str(runtime["observed_at"]))
            observations.add(marker)
            if available > 0:
                markers.add(marker)
        if clear:
            clear_models.add(name)

        normalized["loaded"] = bool(usable)
        normalized["_dynamic_observed"] = bool(usable)
        # This window keeps accepted work visible while engine telemetry
        # catches up, and adds only the dispatch step permitted now.
        accepted = _count(model.get("active")) or 0
        normalized["lane_parallel"] = max(running, accepted) + available if usable else 0
        normalized["max_capacity"] = normalized["lane_parallel"]
        signals["requests_running_current"] = running
        signals["queue_waiting_current"] = waiting
        normalized["scheduler_signals"] = signals

    # A queued engine on one peer is not a saturated model if another ready
    # peer has no wait list. The orchestrator's model-wide backlog still
    # counts: that is user work the runner must leave room for.
    for provider in result["logosnode"]["providers"].values():
        for mid, model in provider["models"].items():
            name = str(model.get("model_name") or mid).strip().lower()
            if name in clear_models:
                model["scheduler_signals"]["queue_waiting_current"] = 0

    known = bool(observed_models) and required_models <= observed_models
    return Snapshot(
        payload=result,
        available_slots=min(headroom.values(), default=0),
        sample_id=tuple(sorted(markers)),
        observation_id=tuple(sorted(observations)),
        known=known,
    )
