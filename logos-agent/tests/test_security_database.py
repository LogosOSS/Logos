"""PostgreSQL regression of incident ownership, deduplication and recovery.

Run with SECURITY_TEST_DATABASE_URL=postgresql+asyncpg://... against a test DB.
Every run uses and removes its own schema; application data is untouched.
"""

import asyncio
import json
import os
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app import security_incidents as incidents
from app import security_store as store
from app.security_models import IncidentInput, RepositoryInput
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("SECURITY_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set"
)


async def test_incident_lifecycle_is_atomic_and_revisits_new_evidence(monkeypatch, tmp_path):
    url = os.environ["SECURITY_TEST_DATABASE_URL"]
    schema = "security_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine)
    monkeypatch.setattr(store.db, "sessionmaker", lambda: factory)
    monkeypatch.setattr(incidents, "upstream_snapshots", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        incidents,
        "settings",
        replace(incidents.settings, artifact_root=str(tmp_path / "artifacts"), state_root=str(tmp_path / "state")),
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""CREATE TABLE agent_workspaces (
                id SERIAL PRIMARY KEY, name text UNIQUE, base_branch text, volume_name text,
                created_by text, ephemeral boolean)"""))
            await conn.execute(text("""CREATE TABLE agent_sessions (
                id SERIAL PRIMARY KEY, workspace_id integer REFERENCES agent_workspaces(id),
                task text, status text, created_by text, open_pull_request boolean,
                deploy_to_dev boolean, screenshot_paths jsonb, no_push boolean, trigger_kind text,
                trigger_ref text, priority integer, priority_reason text, repo_url text, repo_slug text,
                error text, pr_url text)"""))
            migration = (
                Path(__file__).resolve().parents[2]
                / "logos-webservice/src/main/resources/liquibase/changelog/078_security_incidents.xml"
            )
            sql = ET.parse(migration).getroot().find(".//{http://www.liquibase.org/xml/ns/dbchangelog}sql").text
            for statement in sql.split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
        await store.enroll(RepositoryInput(repo_slug="Example/App"), "admin")
        body = IncidentInput(
            advisory_id="CVE-2026-12345",
            title="Critical parser vulnerability",
            severity="critical",
            source_url="https://example.org/advisory",
            summary="A parser can execute untrusted code remotely.",
            importance="Internet-facing services need immediate assessment.",
        )
        first = await store.ingest(body, "admin")
        duplicate = await store.ingest(body, "admin")
        assert duplicate["revision"] == first["revision"] == 1
        await incidents.ensure_assessments()
        await incidents.ensure_assessments()
        due = await incidents.due_assessments()
        assert len(due) == 1
        await asyncio.gather(incidents.queue_assessment(due[0]), incidents.queue_assessment(due[0]))
        overview = await store.overview()
        assessment = overview["assessments"][0]
        sid = assessment["session_id"]
        async with engine.begin() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM agent_sessions"))).scalar_one() == 1
            assert (await conn.execute(text("SELECT no_push FROM agent_sessions"))).scalar_one() is True
            await conn.execute(text("UPDATE agent_sessions SET status='succeeded' WHERE id=:id"), {"id": sid})
        artifact = tmp_path / "artifacts" / str(sid)
        state = tmp_path / "state" / str(sid)
        artifact.mkdir(parents=True)
        state.mkdir(parents=True)
        state.joinpath("checkout_baseline.json").write_text(json.dumps({"checkout_sha": "a" * 40}))
        artifact.joinpath("security-assessment.json").write_text(
            json.dumps(
                {
                    "outcome": "fix_available",
                    "commit_sha": "a" * 40,
                    "rationale": (
                        "The locked parser version is affected and a verified "
                        "upstream release contains the correction."
                    ),
                    "evidence": [
                        {"path": "package-lock.json", "finding": "The parser is locked to the affected version 1.0.0."}
                    ],
                    "recommended_action": "Upgrade the parser to release 1.0.1 and run the parser compatibility tests.",
                    "action": "upgrade",
                    "affected_components": ["parser"],
                    "patched_version": "1.0.1",
                    "patch_source": "https://example.org/releases/1.0.1",
                }
            )
        )
        await incidents.reconcile()
        due = await incidents.due_assessments()
        assert due[0]["status"] == "fix_available"
        await incidents.queue_assessment(due[0])
        async with engine.begin() as conn:
            fix = (await conn.execute(text("SELECT * FROM agent_sessions ORDER BY id DESC LIMIT 1"))).mappings().one()
            assert fix["open_pull_request"] and not fix["no_push"] and not fix["deploy_to_dev"]
            await conn.execute(
                text(
                    "UPDATE agent_sessions SET status='succeeded', "
                    "pr_url='https://github.com/example/app/pull/1' WHERE id=:id"
                ),
                {"id": fix["id"]},
            )
        await incidents.reconcile()
        assert (await store.overview())["assessments"][0]["status"] == "pr_open"
        monkeypatch.setattr(incidents.github, "_get", AsyncMock(return_value={"merged": True, "state": "closed"}))
        await incidents.reconcile()
        assert (await store.overview())["assessments"][0]["status"] == "awaiting_deployment"
        await store.act(
            assessment["id"], "resolve", "Deployed the tested fixed image to all exposed services.", "admin"
        )
        assert not await incidents.due_assessments()
        await store.ingest(
            body.model_copy(update={"summary": "The advisory now includes a second affected runtime variant."}), "admin"
        )
        due = await incidents.due_assessments()
        assert due[0]["incident_revision"] == 2
        # A snapshot made before the update must not launch another fix.
        stale = {**due[0], "incident_revision": 1}
        await incidents.queue_assessment(stale)
        assert len(await incidents.due_assessments()) == 1
        await incidents.queue_assessment(due[0])
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE agent_sessions SET status='succeeded' WHERE status='queued'"))
        await incidents.reconcile()
        assert (await store.overview())["assessments"][0]["status"] == "failed"
        assert len(await store.history(assessment["id"])) >= 7
        await store.act(
            assessment["id"], "recheck", "Supply the missing deployment evidence and retry the analysis.", "admin"
        )
        assert (await incidents.due_assessments())[0]["status"] == "queued"
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()
