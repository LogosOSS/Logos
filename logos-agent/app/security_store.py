"""Durable incident state; schema is owned by the webservice's Liquibase."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from . import db
from .security_models import IncidentInput, RepositoryInput


async def event(conn, assessment_id: int, status: str, detail: dict, by: str = "LogosOSSAgent") -> None:
    await conn.execute(
        text("""INSERT INTO security_assessment_events (assessment_id, status, detail, created_by)
                VALUES (:id, :status, CAST(:detail AS jsonb), :by)"""),
        {"id": assessment_id, "status": status, "detail": json.dumps(detail), "by": by},
    )


async def enroll(body: RepositoryInput, by: str) -> dict:
    async with db.sessionmaker()() as conn:
        row = (
            (
                await conn.execute(
                    text("""
            INSERT INTO security_repositories (repo_slug, branch, verification_command, created_by)
            VALUES (:repo_slug, :branch, :verification_command, :by) ON CONFLICT (repo_slug) DO NOTHING RETURNING *
        """),
                    {**body.model_dump(), "by": by},
                )
            )
            .mappings()
            .first()
        )
        await conn.commit()
    if row is None:
        raise ValueError("Repository is already monitored")
    return dict(row)


async def ingest(body: IncidentInput, by: str, source_data: dict | None = None, *, manual: bool = False) -> dict:
    values = body.model_dump()
    values.update(
        by=by,
        manual=manual,
        upstream_repositories=json.dumps(body.upstream_repositories),
        source_data=json.dumps(source_data or {}),
    )
    async with db.sessionmaker()() as conn:
        # Authenticated manual intake takes precedence regardless of who first
        # reported the incident. Feeds only replace their own non-manual fields.
        row = (
            (
                await conn.execute(
                    text("""
            INSERT INTO security_incidents
                (advisory_id, title, severity, known_exploited, source_url, summary, importance,
                 upstream_repositories, source_data, created_by, manual_override)
            VALUES (:advisory_id, :title, :severity, :known_exploited, :source_url, :summary, :importance,
                    CAST(:upstream_repositories AS jsonb), CAST(:source_data AS jsonb), :by, :manual)
            ON CONFLICT (advisory_id) DO UPDATE SET
                title = EXCLUDED.title, severity = EXCLUDED.severity,
                known_exploited = security_incidents.known_exploited OR EXCLUDED.known_exploited,
                source_url = EXCLUDED.source_url,
                summary = EXCLUDED.summary, importance = EXCLUDED.importance,
                upstream_repositories = EXCLUDED.upstream_repositories,
                source_data = security_incidents.source_data || EXCLUDED.source_data,
                manual_override = security_incidents.manual_override OR EXCLUDED.manual_override,
                revision = security_incidents.revision + 1, updated_at = now()
            WHERE (EXCLUDED.manual_override OR
                   (NOT security_incidents.manual_override AND security_incidents.created_by = EXCLUDED.created_by))
              AND (security_incidents.summary, security_incidents.source_data,
                   security_incidents.upstream_repositories, security_incidents.severity,
                   security_incidents.known_exploited, security_incidents.importance, security_incidents.title,
                   security_incidents.source_url, security_incidents.manual_override)
                  IS DISTINCT FROM (EXCLUDED.summary, security_incidents.source_data || EXCLUDED.source_data,
                                    EXCLUDED.upstream_repositories, EXCLUDED.severity,
                                    security_incidents.known_exploited OR EXCLUDED.known_exploited,
                                    EXCLUDED.importance, EXCLUDED.title, EXCLUDED.source_url,
                                    security_incidents.manual_override OR EXCLUDED.manual_override)
            RETURNING *
        """),
                    values,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            # Supplemental source evidence is independent of display-field
            # ownership. A later GitHub advisory must enrich a scanner/manual
            # incident so subsequent assessments can refresh its patch ranges.
            row = (
                (
                    await conn.execute(
                        text("""
                UPDATE security_incidents SET
                    known_exploited=known_exploited OR :known_exploited,
                    source_data=source_data || CAST(:source_data AS jsonb),
                    revision=revision+1, updated_at=now()
                WHERE advisory_id=:advisory_id AND
                    (known_exploited, source_data) IS DISTINCT FROM
                    (known_exploited OR :known_exploited, source_data || CAST(:source_data AS jsonb))
                RETURNING *
            """),
                        values,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            row = (
                (await conn.execute(text("SELECT * FROM security_incidents WHERE advisory_id=:advisory_id"), values))
                .mappings()
                .one()
            )
        await conn.commit()
    return dict(row)


async def overview() -> dict[str, Any]:
    async with db.sessionmaker()() as conn:
        result = {}
        for key, query in {
            "repositories": "SELECT * FROM security_repositories ORDER BY repo_slug",
            "incidents": "SELECT * FROM security_incidents ORDER BY updated_at DESC, id DESC LIMIT 500",
            "assessments": """SELECT a.*, r.repo_slug, r.branch, s.status AS session_status
                FROM security_assessments a JOIN security_repositories r ON r.id=a.repository_id
                LEFT JOIN agent_sessions s ON s.id=a.session_id
                WHERE a.incident_id IN (SELECT id FROM security_incidents ORDER BY updated_at DESC, id DESC LIMIT 500)
                ORDER BY a.id""",
            "feeds": "SELECT * FROM security_feed_state ORDER BY name",
        }.items():
            result[key] = [dict(row) for row in (await conn.execute(text(query))).mappings()]
    return result


async def history(assessment_id: int) -> list[dict]:
    async with db.sessionmaker()() as conn:
        return [
            dict(r)
            for r in (
                await conn.execute(
                    text("""
            SELECT * FROM security_assessment_events WHERE assessment_id=:id ORDER BY id DESC LIMIT 100
        """),
                    {"id": assessment_id},
                )
            ).mappings()
        ]


async def act(assessment_id: int, action: str, reason: str, by: str) -> bool:
    async with db.sessionmaker()() as conn:
        row = (
            (
                await conn.execute(
                    text("""
            SELECT a.*, s.status AS session_status FROM security_assessments a
            LEFT JOIN agent_sessions s ON s.id=a.session_id WHERE a.id=:id FOR UPDATE OF a
        """),
                    {"id": assessment_id},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        if row["status"] in {"analyzing", "remediating", "pr_open"}:
            raise ValueError("Wait for the current analysis or pull request to finish")
        target = "queued" if action == "recheck" else "resolved"
        revision = row["revision"]
        if target == "resolved":
            # Keep the incident stable through commit. Evidence updates that
            # acquire the row afterwards advance beyond this resolved revision.
            revision = (
                await conn.execute(
                    text("SELECT revision FROM security_incidents WHERE id=:id FOR SHARE"),
                    {"id": row["incident_id"]},
                )
            ).scalar_one()
        await conn.execute(
            text("""UPDATE security_assessments SET status=:status, next_check_at=now(),
            revision=:revision, error=NULL, updated_at=now() WHERE id=:id"""),
            {"id": assessment_id, "status": target, "revision": revision},
        )
        await event(conn, assessment_id, target, {"reason": reason, "revision": revision}, by)
        await conn.commit()
    return True
