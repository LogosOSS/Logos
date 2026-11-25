"""Read-only preview of the next nightly rotation, using scheduler eligibility."""

from datetime import timedelta

from logos.benchmarks.nightly import CAPACITY_WAITING, MAX_PAIRS_PER_NIGHT, schedule_window
from logos.maintenance_window import MaintenanceWindow, maintenance_node_limit


def overview(schedules, scheduler, now, *, recovery_pending=False, claimed_count=None):
    """Preview candidates, not reservations: readiness may change before admission."""
    window = MaintenanceWindow.from_env()
    today = now.date()
    tomorrow = today + timedelta(days=1)
    claimed = claimed_count if claimed_count is not None else sum(row["last_run_date"] == today for row in schedules)
    blocked = recovery_pending or any(row.get("recovery") for row in schedules)
    continuing_today = False
    result = []
    providers = {}
    for row in schedules:
        _, end = schedule_window(row).bounds(now)
        continuing = row["last_run_date"] == today and row.get("status") in {"continuing", CAPACITY_WAITING}
        continuing_today |= row["enabled"] and continuing and now < end
        next_day = tomorrow if now >= end or (claimed >= MAX_PAIRS_PER_NIGHT and not continuing) else today
        state, reason = "disabled", "Nightly runs are disabled"
        recent = row["last_run_date"] is not None and 0 <= (today - row["last_run_date"]).days <= 7
        success = recent and any(item.get("status") == "success" for item in row.get("report", []))
        if success and row["enabled"]:
            state, reason = "recent", "Successful benchmark within the last 7 days"
        if row.get("recovery"):
            state, reason = "blocked", "Worker recovery is pending"
            if row.get("status") in {"running", "preparing"}:
                state, reason = "busy", "Benchmark model is reserved for testing or restoration"
        elif row["enabled"]:
            if blocked:
                state, reason = "blocked", "Nightly rotation waits for the reserved worker"
            elif row["last_run_date"] == next_day and row.get("status") == CAPACITY_WAITING:
                state, reason = "waiting", "Waiting for enough free memory for the benchmark"
            elif row["last_run_date"] == next_day:
                state, reason = (
                    ("next", "Another optimization round in this window; production configuration confirmed")
                    if row.get("status") == "continuing"
                    else (
                        ("recent", "Already tested in this window")
                        if success
                        else ("waiting", "Already attempted in this window")
                    )
                )
            else:
                try:
                    target, _, _ = scheduler.target_lane(row["model_provider_id"], disable_missing=False)
                    providers[row["model_provider_id"]] = target["provider_id"]
                    state, reason = "queued", "Later in the nightly rotation"
                except RuntimeError as exc:
                    state, reason = "blocked", str(exc)
        result.append(
            {
                "model_id": row["model_id"],
                "model_provider_id": row["model_provider_id"],
                "provider_name": row["provider_name"],
                "state": state,
                "reason": reason,
                "last_run_date": row["last_run_date"],
                "recent_success": success,
                "next_date": next_day if row["enabled"] else None,
            }
        )
    for day, remaining in ((today, MAX_PAIRS_PER_NIGHT - claimed), (tomorrow, MAX_PAIRS_PER_NIGHT)):
        if day == tomorrow and claimed >= MAX_PAIRS_PER_NIGHT and continuing_today:
            continue
        pending = [item for item in result if item["state"] == "queued" and item["next_date"] == day]
        while pending and remaining > 0:
            wave = set()
            deferred = []
            for item in pending:
                provider = providers[item["model_provider_id"]]
                if remaining <= 0 or len(wave) >= maintenance_node_limit() or provider in wave:
                    deferred.append(item)
                    continue
                item["state"] = "next"
                item["reason"] = "Candidate for the next window; subject to worker availability and calibration"
                wave.add(provider)
                remaining -= 1
            pending = deferred
    fallback_day = tomorrow if now >= window.bounds(now)[1] or claimed >= MAX_PAIRS_PER_NIGHT else today
    next_day = min((item["next_date"] for item in result if item["next_date"]), default=fallback_day)
    return {"schedules": result, "next_date": next_day, "window": window.describe()}
