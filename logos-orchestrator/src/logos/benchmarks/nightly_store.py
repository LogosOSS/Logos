"""Persistent nightly opt-in, date claims and recovery journal."""

import json
from contextlib import contextmanager

from sqlalchemy import text

from logos.benchmarks.nightly import MAX_PAIRS_PER_NIGHT
from logos.dbutils.dbmanager import DBManager, _init_engine
from logos.maintenance_window import maintenance_node_limit

LOCK_ID = 125500301
MAX_RECOVERY_RESUMES = 2
UNCHANGED = object()


@contextmanager
def cluster_lock():
    """Hold a dedicated session lock through execution and restoration."""
    with _init_engine().connect() as connection:
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}).scalar()
        connection.commit()
        try:
            yield connection if acquired else None
        finally:
            if acquired:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_ID})
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise


def rows() -> list[dict]:
    with DBManager() as db:
        return [
            dict(row)
            for row in db.session.execute(
                text(
                    "SELECT * FROM nightly_benchmarks ORDER BY last_run_date NULLS FIRST, "
                    "CASE WHEN status = 'continuing' THEN updated_at END NULLS FIRST, model_provider_id"
                )
            ).mappings()
        ]


def overview_rows() -> list[dict]:
    """Return public schedule metadata in the scheduler's rotation order."""
    with DBManager() as db:
        return [
            dict(row)
            for row in db.session.execute(
                text(
                    "SELECT n.*, mp.model_id, p.name AS provider_name FROM nightly_benchmarks n "
                    "JOIN model_provider mp ON mp.id = n.model_provider_id "
                    "JOIN providers p ON p.id = mp.provider_id "
                    "ORDER BY n.last_run_date NULLS FIRST, n.model_provider_id"
                )
            ).mappings()
        ]


def status(pair_id: int) -> dict:
    with DBManager() as db:
        row = (
            db.session.execute(
                text(
                    "SELECT model_provider_id, enabled, last_run_date, status, report, workload_id, optimizer, "
                    "recovery IS NOT NULL AS recovery_required FROM nightly_benchmarks WHERE model_provider_id = :id"
                ),
                {"id": pair_id},
            )
            .mappings()
            .first()
        )
        return (
            dict(row)
            if row
            else {
                "model_provider_id": pair_id,
                "enabled": False,
                "status": "disabled",
                "report": [],
                "recovery_required": False,
                "workload_id": None,
                "optimizer": {},
            }
        )


def set_enabled(pair_id: int, enabled: bool, expected: bool, workload_id=UNCHANGED, expected_workload_id=None) -> dict:
    """Compare-and-set prevents a stale confirmation from toggling newer state."""
    with DBManager() as db:
        db.session.execute(
            text("INSERT INTO nightly_benchmarks(model_provider_id) VALUES (:id) ON CONFLICT DO NOTHING"),
            {"id": pair_id},
        )
        changed = db.session.execute(
            text(
                "UPDATE nightly_benchmarks SET enabled = :enabled, updated_at = CURRENT_TIMESTAMP, "
                "workload_id = CASE WHEN :change_workload THEN :workload_id ELSE workload_id END, "
                "optimizer = CASE WHEN :change_workload AND workload_id IS DISTINCT FROM :workload_id "
                "THEN '{}'::jsonb ELSE optimizer END, "
                "status = CASE WHEN recovery IS NOT NULL THEN status "
                "WHEN status = 'continuing' THEN status WHEN :enabled THEN 'scheduled' ELSE 'disabled' END "
                "WHERE model_provider_id = :id AND enabled = :expected AND (NOT :enabled OR recovery IS NULL) "
                "AND (NOT :change_workload OR (workload_id IS NOT DISTINCT FROM :expected_workload "
                "AND recovery IS NULL AND status NOT IN ('running', 'preparing', 'continuing', 'recovery_required')))"
            ),
            {
                "id": pair_id,
                "enabled": enabled,
                "expected": expected,
                "change_workload": workload_id is not UNCHANGED,
                "workload_id": None if workload_id is UNCHANGED else workload_id,
                "expected_workload": expected_workload_id,
            },
        )
        if not changed.rowcount:
            raise ValueError("Nightly state changed or recovery is required. Refresh before confirming.")
        db.session.commit()
    return status(pair_id)


def claim(pair_id: int, day) -> bool:
    """Claim a new pair or another restored round; only distinct pairs consume the daily quota."""
    with DBManager() as db:
        claimed = bool(
            db.session.execute(
                text(
                    "UPDATE nightly_benchmarks SET last_run_date = :day, status = 'running', report = '[]'::jsonb "
                    "WHERE model_provider_id = :id AND enabled AND recovery IS NULL "
                    "AND ((last_run_date = :day AND status = 'continuing') OR "
                    "((last_run_date IS NULL OR last_run_date < :day) "
                    "AND (SELECT COUNT(*) FROM nightly_benchmarks WHERE last_run_date = :day) < :limit))"
                ),
                {"id": pair_id, "day": day, "limit": MAX_PAIRS_PER_NIGHT},
            ).rowcount
        )

        db.session.commit()
        return claimed


def bind_workload(pair_id, workload_id):
    """Bind the admitted night's inputs without discarding its previous search parameters."""
    with DBManager() as db:
        changed = db.session.execute(
            text(
                "UPDATE nightly_benchmarks SET workload_id = :workload WHERE model_provider_id = :id "
                "AND recovery IS NULL AND status = 'running'"
            ),
            {"id": pair_id, "workload": workload_id},
        )
        if not changed.rowcount:
            raise ValueError("The nightly schedule is no longer available for source selection")
        db.session.commit()


def claimed_today(day) -> int:
    with DBManager() as db:
        return db.session.execute(
            text("SELECT COUNT(*) FROM nightly_benchmarks WHERE last_run_date = :day"), {"day": day}
        ).scalar_one()


def _enqueue_completion_event(db, event_id: str, payload: dict) -> None:
    db.session.execute(
        text(
            "INSERT INTO benchmark_completion_events(event_id, payload) "
            "VALUES (:event, CAST(:payload AS jsonb)) ON CONFLICT (event_id) DO NOTHING"
        ),
        {"event": event_id, "payload": json.dumps(payload)},
    )


def save(
    pair_id: int, state: str, report: list, recovery: dict | None, *, adoption_failure: dict | None = None
) -> None:
    with DBManager() as db:
        db.session.execute(
            text(
                "UPDATE nightly_benchmarks SET status = :state, report = CAST(:report AS jsonb), "
                "recovery = CAST(:recovery AS jsonb), updated_at = CURRENT_TIMESTAMP WHERE model_provider_id = :id"
            ),
            {
                "id": pair_id,
                "state": state,
                "report": json.dumps(report),
                "recovery": json.dumps(recovery) if recovery is not None else None,
            },
        )

        if adoption_failure is not None:
            target = (
                db.session.execute(
                    text(
                        "SELECT mp.model_id, p.name AS provider_name FROM model_provider mp "
                        "JOIN providers p ON p.id = mp.provider_id WHERE mp.id = :id"
                    ),
                    {"id": pair_id},
                )
                .mappings()
                .first()
            )
            if target is not None:
                _enqueue_completion_event(
                    db,
                    f"nightly-adoption-failed:{pair_id}:{adoption_failure['benchmark_id']}",
                    {**adoption_failure, **target, "model_provider_id": pair_id},
                )
        db.session.commit()


def has_active_benchmarks(excluding: list[int] | None = None) -> bool:
    with DBManager() as db:
        return bool(
            db.session.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM jobs WHERE environment = 'model-provider-benchmark' "
                    "AND status IN ('pending', 'running') AND NOT (id = ANY(CAST(:excluded AS integer[])))) "
                    "OR EXISTS(SELECT 1 FROM maintenance_benchmark)"
                ),
                {"excluded": excluding or []},
            ).scalar()
        )


def manual_admission(db, provider_id: int) -> bool:
    """Serialize admission against nightly execution, including recovery after a crash."""
    acquired = db.session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}).scalar()
    return (
        bool(acquired)
        and not db.session.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM nightly_benchmarks WHERE recovery IS NOT NULL) "
                "OR EXISTS(SELECT 1 FROM maintenance_calibration) "
                "OR EXISTS(SELECT 1 FROM maintenance_benchmark WHERE provider_id = :provider OR provider_id IS NULL) "
                "OR EXISTS(SELECT 1 FROM jobs WHERE environment = 'model-provider-benchmark' "
                "AND status IN ('pending', 'running') AND request_payload->>'provider_id' = :provider_text) "
                "OR (SELECT COUNT(*) FROM ("
                "SELECT id FROM jobs WHERE environment = 'model-provider-benchmark' "
                "AND status IN ('pending', 'running') UNION SELECT job_id FROM maintenance_benchmark"
                ") AS active_jobs) >= :limit"
            ),
            {"limit": maintenance_node_limit(), "provider": provider_id, "provider_text": str(provider_id)},
        ).scalar()
    )


def lock_held(connection) -> bool:
    """A reconnected SQL session must not continue under a lost advisory lock."""
    if connection.invalidated or connection.closed:
        return False
    try:
        held = connection.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid = 0 AND objid = :key AND granted)"
            ),
            {"key": LOCK_ID},
        ).scalar()
        connection.commit()
        return bool(held)
    except Exception:
        return False


def write_progress(job_id: int, completed: int, total: int) -> None:
    with DBManager() as db:
        db.session.execute(
            text(
                "UPDATE jobs SET result_payload = COALESCE(result_payload, '{}'::jsonb) || CAST(:progress AS jsonb) "
                "WHERE id = :id AND status = 'running'"
            ),
            {"id": job_id, "progress": json.dumps({"completed_samples": completed, "total_samples": total})},
        )

        db.session.commit()


def disable_missing_target(pair_id: int) -> None:
    with DBManager() as db:
        db.session.execute(
            text("UPDATE nightly_benchmarks SET enabled = false WHERE model_provider_id = :id"), {"id": pair_id}
        )
        db.session.commit()


def has_recovery():
    with DBManager() as db:
        return bool(
            db.session.execute(
                text("SELECT EXISTS(SELECT 1 FROM nightly_benchmarks WHERE recovery IS NOT NULL)")
            ).scalar()
        )


def calibration_claims():
    with DBManager() as db:
        return [
            dict(row)
            for row in db.session.execute(
                # The age by the database clock, the one created_at was set by.
                text(
                    "SELECT *, EXTRACT(EPOCH FROM now() - created_at) AS age_s "
                    "FROM maintenance_calibration ORDER BY provider_id"
                )
            ).mappings()
        ]


def maintenance_holders():
    """Calibration claims, maintenance benchmarks and nightly recoveries, with names for display."""
    with DBManager() as db:
        calibrations = db.session.execute(
            text(
                "SELECT c.*, EXTRACT(EPOCH FROM now() - c.created_at) AS age_s, p.name AS provider_name "
                "FROM maintenance_calibration c LEFT JOIN providers p ON p.id = c.provider_id ORDER BY c.provider_id"
            )
        ).mappings()
        calibrations = [dict(row) for row in calibrations]
        # The same jobs and claims has_active_benchmarks counts. A claimed job
        # that already ended keeps its metadata while its cleanup runs.
        benchmarks = db.session.execute(
            text(
                "SELECT COALESCE(j.id, b.job_id) AS job_id, j.status, "
                "COALESCE(b.created_at, j.created_at) AS since, "
                "EXTRACT(EPOCH FROM now() - COALESCE(b.created_at, j.created_at)) AS age_s, "
                "COALESCE(b.provider_id, (j.request_payload->>'provider_id')::integer) AS provider_id, "
                "COALESCE(p.name, j.request_payload->>'provider_name') AS provider_name, "
                "j.request_payload->>'model_name' AS model_name, "
                "COALESCE(j.request_payload->>'nightly', 'false') = 'true' AS nightly "
                "FROM (SELECT * FROM jobs WHERE environment = 'model-provider-benchmark' "
                "AND (status IN ('pending', 'running') OR id IN (SELECT job_id FROM maintenance_benchmark))) AS j "
                "FULL OUTER JOIN maintenance_benchmark AS b ON b.job_id = j.id "
                "LEFT JOIN providers p ON p.id = COALESCE(b.provider_id, (j.request_payload->>'provider_id')::integer) "
                "ORDER BY since, job_id"
            )
        ).mappings()
        benchmarks = [dict(row) for row in benchmarks]
        recoveries = db.session.execute(
            text(
                "SELECT n.model_provider_id, n.status, n.updated_at AS since, "
                "EXTRACT(EPOCH FROM now() - n.updated_at) AS age_s, "
                "COALESCE(mp.provider_id, (n.recovery->>'provider_id')::integer) AS provider_id, "
                "p.name AS provider_name, COALESCE(m.name, n.recovery->>'model') AS model_name "
                "FROM nightly_benchmarks n LEFT JOIN model_provider mp ON mp.id = n.model_provider_id "
                "LEFT JOIN providers p ON p.id = mp.provider_id LEFT JOIN models m ON m.id = mp.model_id "
                "WHERE n.recovery IS NOT NULL ORDER BY n.model_provider_id"
            )
        ).mappings()
        recoveries = [dict(row) for row in recoveries]
    return {"calibrations": calibrations, "benchmarks": benchmarks, "recoveries": recoveries}


def claim_calibration(provider_id, token, automatic=False):
    with DBManager() as db:
        db.session.execute(
            text(
                "INSERT INTO maintenance_calibration (provider_id, token, automatic) VALUES (:pid, :token, :automatic)"
            ),
            {"pid": provider_id, "token": token, "automatic": automatic},
        )
        db.session.commit()


def confirm_calibration(token):
    with DBManager() as db:
        db.session.execute(
            text("UPDATE maintenance_calibration SET confirmed = true WHERE token = :token"), {"token": token}
        )
        db.session.commit()


def release_calibration(token):
    with DBManager() as db:
        db.session.execute(text("DELETE FROM maintenance_calibration WHERE token = :token"), {"token": token})
        db.session.commit()


def claim_benchmark(db, job_id, provider_id):
    db.session.execute(
        text("INSERT INTO maintenance_benchmark (job_id, provider_id) VALUES (:job, :provider)"),
        {"job": job_id, "provider": provider_id},
    )
    db.session.commit()


def release_benchmark(job_id):
    with DBManager() as db:
        db.session.execute(text("DELETE FROM maintenance_benchmark WHERE job_id = :job"), {"job": job_id})
        db.session.commit()


@contextmanager
def execution_lock(pair_id: int):
    """Hold a separate execution lease until the nightly task has finished cleanup."""
    with _init_engine().connect() as connection:
        acquired = connection.execute(
            text("SELECT pg_try_advisory_lock(:namespace, :pair)"), {"namespace": LOCK_ID, "pair": pair_id}
        ).scalar()
        connection.commit()
        try:
            yield connection if acquired else None
        finally:
            if acquired:
                try:
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:namespace, :pair)"),
                        {"namespace": LOCK_ID, "pair": pair_id},
                    )
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise


def execution_lock_held(connection, pair_id: int) -> bool:
    if connection.invalidated or connection.closed:
        return False
    try:
        held = connection.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND classid = :namespace AND objid = :pair AND objsubid = 2 AND granted)"
            ),
            {"namespace": LOCK_ID, "pair": pair_id},
        ).scalar()
        connection.commit()
        return bool(held)
    except Exception:
        return False


def recovery_jobs(pair_id: int) -> list[int]:
    """Only jobs explicitly owned by this nightly schedule are recovery candidates."""
    with DBManager() as db:
        return list(
            db.session.execute(
                text(
                    "SELECT id FROM jobs WHERE environment = 'model-provider-benchmark' "
                    "AND status IN ('pending', 'running') AND request_payload->>'nightly' = 'true' "
                    "AND request_payload->>'model_provider_id' = :pair"
                ),
                {"pair": str(pair_id)},
            ).scalars()
        )


def fence_recovery_jobs(job_ids: list[int]) -> None:
    """Stop old runners and queued inference from admitting further worker requests."""
    with DBManager() as db:
        db.session.execute(
            text(
                "UPDATE jobs SET request_payload = request_payload || jsonb_build_object('recovery_fenced', true) "
                "WHERE id = ANY(CAST(:ids AS integer[]))"
            ),
            {"ids": job_ids},
        )
        db.session.commit()


def benchmark_requests_allowed(job_id: int) -> bool:
    with DBManager() as db:
        return bool(
            db.session.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM jobs WHERE id = :id AND status = 'running' "
                    "AND COALESCE(request_payload->>'recovery_fenced', 'false') <> 'true')"
                ),
                {"id": job_id},
            ).scalar()
        )


def finish_recovery_jobs(job_ids: list[int]) -> None:
    with DBManager() as db:
        db.session.execute(
            text(
                "UPDATE jobs SET status = 'failed', error_message = 'Nightly execution interrupted; worker drained', "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ANY(CAST(:ids AS integer[])) "
                "AND request_payload->>'recovery_fenced' = 'true'"
            ),
            {"ids": job_ids},
        )
        db.session.commit()


def record_comparison(benchmark_id, baseline_id, parameters, original, search=None):
    """Attach an explicit same-night baseline; never infer a comparison from dates."""
    with DBManager() as db:
        db.session.execute(
            text(
                "UPDATE model_provider_benchmarks SET configuration = configuration || CAST(:comparison AS jsonb) "
                "WHERE id = :id"
            ),
            {
                "id": benchmark_id,
                "comparison": json.dumps(
                    {
                        "nightly": {
                            "baseline_id": baseline_id,
                            "parameters": parameters,
                            "original": {key: original.get(key) for key in parameters},
                            **(
                                {
                                    "reference_id": search["reference_id"],
                                    "series_key": search["series_key"],
                                    "workload_id": search["workload_id"],
                                    "source_selection": search.get("source_selection"),
                                }
                                if search
                                else {}
                            ),
                        }
                    }
                ),
            },
        )
        db.session.commit()


def adoption_candidate(pair_id, search, report):
    """Read successful measurements for the reserved deployment before adoption."""
    from logos.benchmarks import optimization

    ids = [item["benchmark_id"] for item in report if item.get("status") == "success" and item.get("benchmark_id")]
    if not ids:
        return None
    with DBManager() as db:
        runs = list(
            db.session.execute(
                text(
                    "SELECT id, configuration, metrics, sample_size FROM model_provider_benchmarks "
                    "WHERE model_provider_id = :pair AND id = ANY(:ids)"
                ),
                {"pair": pair_id, "ids": ids},
            ).mappings()
        )
    return optimization.adoption_candidate(search, runs, report)


def selection_summary(pair_id, workload_id=None):
    """Return bounded comparison diagnostics without request or response content."""
    from logos.benchmarks.optimization import eligibility_issues, rate

    with DBManager() as db:
        runs = list(
            db.session.execute(
                text(
                    "SELECT id, configuration, metrics, sample_size FROM model_provider_benchmarks "
                    "WHERE model_provider_id = :pair ORDER BY id DESC LIMIT 100"
                ),
                {"pair": pair_id},
            ).mappings()
        )
    by_id = {run["id"]: run for run in runs}
    if not workload_id and runs:
        workload_id = runs[0]["configuration"].get("workload", {}).get("id")
    comparisons = []
    for run in runs:
        if not workload_id or run["configuration"].get("workload", {}).get("id") != workload_id:
            continue
        nightly = run["configuration"].get("nightly") or {}
        baseline = by_id.get(nightly.get("baseline_id"))
        issues = eligibility_issues(run, baseline) if baseline else ["No comparison baseline was recorded"]
        comparisons.append(
            {
                "benchmark_id": run["id"],
                "baseline_id": nightly.get("baseline_id"),
                "throughput": rate(run),
                "issues": issues,
            }
        )
    observed = [row for row in comparisons if row["throughput"] is not None]
    eligible = [row for row in observed if not row["issues"]]
    return {
        "best_observed": max(observed, key=lambda row: row["throughput"], default=None),
        "best_eligible": max(eligible, key=lambda row: row["throughput"], default=None),
        "comparisons": comparisons,
    }


def finish(pair_id, state, report, search=None, *, resume_after_recovery=False):
    """Commit restored state, the observed winner and its completion event together."""
    from logos.benchmarks.completion import result
    from logos.benchmarks.optimization import eligibility_issues, select_state

    with DBManager() as db:
        row = (
            db.session.execute(
                text(
                    "SELECT n.last_run_date, n.status, n.optimizer, n.enabled, mp.model_id, p.name AS provider_name "
                    "FROM nightly_benchmarks n "
                    "JOIN model_provider mp ON mp.id = n.model_provider_id JOIN providers p ON p.id = mp.provider_id "
                    "WHERE n.model_provider_id = :id FOR UPDATE OF n"
                ),
                {"id": pair_id},
            )
            .mappings()
            .one()
        )
        previous = row["optimizer"] or {}
        session = dict(previous.get("nightly_session") or {})
        same_night = session.get("date") == str(row["last_run_date"])
        if not same_night:
            session = {"date": str(row["last_run_date"]), "rounds": 0, "successful_configurations": 0}
        optimizer = None
        if search:
            ids = [item["benchmark_id"] for item in report if item.get("benchmark_id")]
            runs = (
                list(
                    db.session.execute(
                        text(
                            "SELECT id, configuration, metrics, sample_size FROM model_provider_benchmarks "
                            "WHERE id = ANY(:ids)"
                        ),
                        {"ids": ids},
                    ).mappings()
                )
                if ids
                else []
            )
            optimizer = select_state(search, runs, report)
            optimizer["last_round_result"] = result(runs, report) or (
                previous.get("last_round_result", {}) if same_night else {}
            )
            adopted = next((item for item in report if item.get("status") == "adopted"), None)
            if adopted:
                if not row["enabled"]:
                    raise RuntimeError("Nightly schedule disabled before adoption committed")
                optimizer["production_adoption"] = adopted
                session["production_adoption"] = adopted
            baseline_id = next(
                (
                    item.get("benchmark_id")
                    for item in report
                    if item.get("configuration") == 1 and item.get("status") == "success"
                ),
                None,
            )
            baseline = next((run for run in runs if run["id"] == baseline_id), None)
            for run in runs:
                comparison = dict(run["configuration"].get("nightly") or {})
                comparison["selection_issues"] = (
                    eligibility_issues(run, baseline) if baseline else ["No comparison baseline was recorded"]
                )
                comparison["eligible_for_selection"] = not comparison["selection_issues"]
                db.session.execute(
                    text(
                        "UPDATE model_provider_benchmarks SET configuration = "
                        "jsonb_set(configuration, '{nightly}', CAST(:nightly AS jsonb)) WHERE id = :id"
                    ),
                    {"id": run["id"], "nightly": json.dumps(comparison)},
                )
        if row["last_run_date"] is not None and row["status"] in {"running", "preparing", "recovery_required"}:
            if optimizer is None:
                optimizer = dict(previous)
                if not same_night:
                    optimizer.pop("last_round_result", None)
            session["rounds"] += 1
            session["successful_configurations"] += sum(item.get("status") == "success" for item in report)
            optimizer["nightly_session"] = session
        if state.startswith("interrupted") or state == "failed":
            reason = next((item["reason"] for item in reversed(report) if item.get("reason")), None)
            if reason:
                session["interruption_reason"] = reason
            elif state.startswith("interrupted"):
                session["interruption_reason"] = "The previous execution ended before worker restoration was confirmed."
        if state == "interrupted_recovered" and optimizer is not None:
            if resume_after_recovery and row["enabled"]:
                resumes = session.get("recovery_resumes", 0)
                if resumes < MAX_RECOVERY_RESUMES:
                    session["recovery_resumes"] = resumes + 1
                    state = "continuing"
                else:
                    session["recovery_retry_limit_reached"] = True
        db.session.execute(
            text(
                "UPDATE nightly_benchmarks SET status = :state, report = CAST(:report AS jsonb), recovery = NULL, "
                "optimizer = COALESCE(CAST(:optimizer AS jsonb), optimizer), updated_at = CURRENT_TIMESTAMP "
                "WHERE model_provider_id = :id"
            ),
            {
                "id": pair_id,
                "state": state,
                "report": json.dumps(report),
                "optimizer": json.dumps(optimizer) if optimizer is not None else None,
            },
        )
        if row["last_run_date"] is not None and state != "continuing":
            event_optimizer = optimizer or row["optimizer"] or {}
            payload = {
                "model_provider_id": pair_id,
                "model_id": row["model_id"],
                "provider_name": row["provider_name"],
                "date": str(row["last_run_date"]),
                "status": state,
                "successful_configurations": event_optimizer.get("nightly_session", {}).get(
                    "successful_configurations", sum(item.get("status") == "success" for item in report)
                ),
                "rounds": event_optimizer.get("nightly_session", {}).get("rounds", 1),
                "reference_id": event_optimizer.get("reference_id"),
                "best_benchmark_id": event_optimizer.get("best_benchmark_id"),
                "production_adoption": session.get("production_adoption"),
                "interruption_reason": session.get("interruption_reason"),
                "recovery_resumes": session.get("recovery_resumes", 0),
                "recovery_retry_limit_reached": session.get("recovery_retry_limit_reached", False),
                "benchmark_id": None,
                **event_optimizer.get("last_round_result", {}),
            }
            _enqueue_completion_event(db, f"nightly:{pair_id}:{row['last_run_date']}", payload)
        db.session.commit()


def finalize_continuations(now):
    """Publish the final result only after all restored rounds end, including after a restart."""
    from logos.maintenance_window import MaintenanceWindow

    with cluster_lock() as connection:
        if connection is None:
            return
        for row in rows():
            if row["status"] != "continuing" or row["recovery"] is not None:
                continue
            if (
                not row["enabled"]
                or row["last_run_date"] != now.date()
                or not MaintenanceWindow.from_env().contains(now)
            ):
                finish(row["model_provider_id"], "completed" if row["enabled"] else "disabled", row["report"])


async def deliver_completion_events():
    """Retry persisted events; the application server deduplicates each event key."""
    from logos.benchmarks.completion import notification_message
    from logos.notifications import emit_notification

    with DBManager() as db:
        events = list(
            db.session.execute(
                text(
                    "SELECT event_id, payload FROM benchmark_completion_events WHERE delivered_at IS NULL "
                    "ORDER BY created_at LIMIT 20"
                )
            ).mappings()
        )
    for event in events:
        payload = event["payload"]
        title, body = notification_message(payload)
        delivered = await emit_notification(
            "benchmark_result_ready",
            title,
            body,
            payload,
            [{"kind": "role", "role": "logos_admin"}],
            event_key=event["event_id"],
        )
        if delivered:
            with DBManager() as db:
                db.session.execute(
                    text(
                        "UPDATE benchmark_completion_events SET delivered_at = CURRENT_TIMESTAMP "
                        "WHERE event_id = :event"
                    ),
                    {"event": event["event_id"]},
                )
                db.session.commit()
