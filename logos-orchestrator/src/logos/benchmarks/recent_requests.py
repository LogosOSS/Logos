"""One immutable source selection per night, shared by every tested model."""

import hashlib
import os

from sqlalchemy import text

from logos.benchmarks.workloads import MAX_BYTES, canonical, replay_request
from logos.dbutils.dbmanager import DBManager
from logos.maintenance_window import MaintenanceWindow

REQUEST_COUNT = 50


def username():
    """An empty deployment setting keeps manually selected workloads unchanged."""
    return os.getenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "").strip()


def policy(now=None):
    owner = username()
    result = {
        "kind": "recent_user" if owner else "selected",
        "username": owner or None,
        "request_count": REQUEST_COUNT if owner else None,
    }
    if owner and now is not None:
        cutoff = MaintenanceWindow.from_env().bounds(now)[0]
        with DBManager() as db:
            row = (
                db.session.execute(
                    text(
                        "SELECT user_id, cutoff, error, requests IS NOT NULL AS ready FROM nightly_request_sources "
                        "WHERE day = :day AND username = :owner"
                    ),
                    {"day": cutoff.date(), "owner": owner},
                )
                .mappings()
                .first()
            )
        result.update(dict(row) if row else {"ready": False, "cutoff": cutoff, "error": None})
    return result


def freeze(now, *, include_requests=True):
    """Freeze before calibration; a restart or failed selection never replaces this night's inputs."""
    owner = username()
    window = MaintenanceWindow.from_env()
    if not owner or not window.contains(now):
        return None
    cutoff = window.bounds(now)[0]
    with DBManager() as db:
        if not db.session.execute(text("SELECT EXISTS(SELECT 1 FROM nightly_benchmarks WHERE enabled)")).scalar():
            return None
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"nightly-source:{cutoff.date()}:{owner}"},
        )
        fields = "*" if include_requests else "day, username, user_id, cutoff, error"
        existing = (
            db.session.execute(
                text(f"SELECT {fields} FROM nightly_request_sources WHERE day = :day AND username = :owner"),
                {"day": cutoff.date(), "owner": owner},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return dict(existing)
        users = list(
            db.session.execute(text("SELECT id FROM users WHERE username = :owner"), {"owner": owner}).scalars()
        )
        user_id = users[0] if len(users) == 1 else None
        requests = None
        error = None
        if user_id is None:
            error = "The configured source username must resolve to exactly one user"
        else:
            rows = list(
                db.session.execute(
                    text(
                        "SELECT id, input_payload FROM log_entry WHERE user_id = :user_id "
                        "AND privacy_level = 'FULL' AND result_status = 'success' AND input_payload IS NOT NULL "
                        "AND environment IS DISTINCT FROM 'model-provider-benchmark' "
                        "AND timestamp_request <= :cutoff AND timestamp_response <= :cutoff "
                        "ORDER BY timestamp_request DESC, id DESC LIMIT 50"
                    ),
                    {"user_id": user_id, "cutoff": cutoff},
                ).mappings()
            )
            if len(rows) != REQUEST_COUNT:
                error = f"Only {len(rows)} of 50 successful FULL requests exist before the nightly cutoff"
            else:
                try:
                    requests = [{"source_log_id": row["id"], **replay_request(row["input_payload"])} for row in rows]
                    if len(canonical(requests).encode()) > MAX_BYTES:
                        raise ValueError("The nightly request selection exceeds 5 MB")
                except ValueError as exc:
                    error = str(exc)
                    requests = None
        db.session.execute(
            text(
                "INSERT INTO nightly_request_sources(day, username, user_id, cutoff, requests, error) "
                "VALUES (:day, :owner, :user_id, :cutoff, CAST(:requests AS jsonb), :error)"
            ),
            {
                "day": cutoff.date(),
                "owner": owner,
                "user_id": user_id,
                "cutoff": cutoff,
                "requests": canonical(requests) if requests is not None else None,
                "error": error,
            },
        )
        db.session.commit()
        return {
            "day": cutoff.date(),
            "username": owner,
            "user_id": user_id,
            "cutoff": cutoff,
            "requests": requests,
            "error": error,
        }


def for_model(target, now):
    """Bind the same private source inputs to the model being measured, irrespective of source model."""
    source = freeze(now)
    if not source or source["error"]:
        raise ValueError(source["error"] if source else "The nightly request selection is unavailable")
    content = {"model_id": target["model_id"], "provenance": "production", "requests": source["requests"]}
    identifier = hashlib.sha256(canonical(content).encode()).hexdigest()
    name = f"Recent 50 FULL requests · {source['username']}"[:128]
    with DBManager() as db:
        db.session.execute(
            text(
                "INSERT INTO benchmark_workloads(id, name, model_id, model_name, provenance, requests, request_count) "
                "VALUES (:id, :name, :model_id, :model_name, 'production', CAST(:requests AS jsonb), 50) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": identifier,
                "name": name,
                "model_id": target["model_id"],
                "model_name": target["model_name"],
                "requests": canonical(source["requests"]),
            },
        )
        db.session.commit()
        name = db.session.execute(
            text("SELECT name FROM benchmark_workloads WHERE id = :id"), {"id": identifier}
        ).scalar_one()
    return {
        "id": identifier,
        "name": name,
        "model_name": target["model_name"],
        "request_count": 50,
        **content,
        "source_selection": {
            "username": source["username"],
            "user_id": source["user_id"],
            "cutoff": source["cutoff"].isoformat(),
            "day": str(source["day"]),
        },
    }
