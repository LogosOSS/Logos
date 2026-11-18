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

import httpx
import pytest
from app import auth, main
from app import security_incidents as incidents
from app import security_scans
from app import security_store as store
from app.security_models import IncidentInput, RepositoryInput
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    not os.getenv("SECURITY_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set"
)


@pytest.fixture
async def database(monkeypatch, tmp_path):
    url = os.environ["SECURITY_TEST_DATABASE_URL"]
    schema = "security_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine)
    monkeypatch.setattr(store.db, "sessionmaker", lambda: factory)
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
            changelogs = Path(__file__).resolve().parents[2] / "logos-webservice/src/main/resources/liquibase/changelog"
            for name in ("078_security_incidents.xml", "079_security_manual_updates.xml"):
                sql = (
                    ET.parse(changelogs / name)
                    .getroot()
                    .find(".//{http://www.liquibase.org/xml/ns/dbchangelog}sql")
                    .text
                )
                for statement in sql.split(";"):
                    if statement.strip():
                        await conn.execute(text(statement))
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


def incident_body(**updates):
    return IncidentInput(
        advisory_id="CVE-2026-12345",
        title="Critical parser vulnerability",
        severity="critical",
        source_url="https://example.org/advisory",
        summary="A parser can execute untrusted code remotely.",
        importance="Internet-facing services need immediate assessment.",
    ).model_copy(update=updates)


async def test_incident_lifecycle_is_atomic_and_revisits_new_evidence(database, monkeypatch, tmp_path):
    engine = database
    await store.enroll(RepositoryInput(repo_slug="Example/App"), "admin")
    body = incident_body()
    first = await store.ingest(body, "admin", manual=True)
    duplicate = await store.ingest(body, "admin", manual=True)
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
                    "The locked parser version is affected and a verified " "upstream release contains the correction."
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
    await store.act(assessment["id"], "resolve", "Deployed the tested fixed image to all exposed services.", "admin")
    assert not await incidents.due_assessments()
    await store.ingest(
        body.model_copy(update={"summary": "The advisory now includes a second affected runtime variant."}),
        "admin",
        manual=True,
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


async def test_manual_evidence_supersedes_any_creator_and_survives_feed_polls(database):
    original = incident_body()
    first = await store.ingest(original, "feed:github", {"ghsa_id": "GHSA-2345-6789-abcd"})
    changed = incident_body(
        summary="Vendor confirmed a second affected service behind our edge proxy.",
        upstream_repositories=["vendor/server"],
    )
    manual = await store.ingest(changed, "admin-one", manual=True)
    assert manual["revision"] == first["revision"] + 1
    assert manual["created_by"] == "feed:github"
    assert manual["manual_override"] is True
    assert manual["upstream_repositories"] == ["vendor/server"]
    assert manual["source_data"]["ghsa_id"] == "GHSA-2345-6789-abcd"
    second = await store.ingest(
        changed.model_copy(update={"importance": "Multiple public services share this image."}),
        "admin-two",
        manual=True,
    )
    assert second["revision"] == manual["revision"] + 1
    for creator in ("feed:github", "feed:cisa", "scanner:trivy"):
        polled = await store.ingest(original, creator)
        assert polled == second
    assert (
        await store.ingest(changed.model_copy(update={"importance": second["importance"]}), "admin-three", manual=True)
    )["revision"] == second["revision"]
    other = await store.ingest(incident_body(advisory_id="CVE-2026-23456"), "admin-one", manual=True)
    assert (
        await store.ingest(
            incident_body(advisory_id="CVE-2026-23456", summary=changed.summary), "admin-two", manual=True
        )
    )["revision"] == other["revision"] + 1


async def test_resolving_queued_or_stale_assessment_accepts_current_evidence(database):
    await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    first = await store.ingest(incident_body(), "admin", manual=True)
    await incidents.ensure_assessments()
    row = (await incidents.due_assessments())[0]
    assert row["revision"] == 0
    assert await store.act(row["id"], "resolve", "All deployments were independently verified.", "admin")
    assert not await incidents.due_assessments()
    assert (await store.history(row["id"]))[0]["detail"]["revision"] == first["revision"]
    await store.ingest(incident_body(summary="Additional versions are now listed by the vendor."), "admin", manual=True)
    assert await incidents.due_assessments()
    assert await store.act(row["id"], "resolve", "Also verified the newly listed versions.", "admin")
    assert not await incidents.due_assessments()


async def test_resolution_serializes_with_concurrent_evidence_updates(database, monkeypatch):
    await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    await store.ingest(incident_body(), "admin", manual=True)
    await incidents.ensure_assessments()
    row = (await incidents.due_assessments())[0]
    event_started, release = asyncio.Event(), asyncio.Event()
    real_event = store.event

    async def blocked_event(*args, **kwargs):
        event_started.set()
        await release.wait()
        await real_event(*args, **kwargs)

    monkeypatch.setattr(store, "event", blocked_event)
    resolving = asyncio.create_task(store.act(row["id"], "resolve", "Verified deployments.", "admin"))
    await asyncio.wait_for(event_started.wait(), timeout=5)
    update = asyncio.create_task(
        store.ingest(incident_body(summary="The vendor expanded the affected runtime scope."), "admin", manual=True)
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(update), timeout=0.1)
    finally:
        release.set()
        await asyncio.gather(resolving, update)
    due = await incidents.due_assessments()
    assert due[0]["revision"] == 1
    assert due[0]["incident_revision"] == 2


async def test_reconciliation_visits_later_sessions_despite_open_prs(database, monkeypatch):
    await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    async with database.begin() as conn:
        await conn.execute(text("""INSERT INTO security_incidents
            (advisory_id, title, severity, source_url, summary, importance, created_by)
            SELECT 'CVE-2026-' || n, 'Parser', 'critical', 'https://example.org', 'Details', 'Critical', 'admin'
            FROM generate_series(10000, 10201) n"""))
    await incidents.ensure_assessments()
    async with database.begin() as conn:
        await conn.execute(text("""UPDATE security_assessments SET status='pr_open',
            pr_url='https://github.com/example/app/pull/' || id WHERE id <= 201"""))
        await conn.execute(text("UPDATE security_assessments SET status='analyzing' WHERE id=202"))

    async def pr_state(path):
        if path.endswith("/1"):
            raise incidents.github.GitHubError("Temporary upstream error")
        return {"state": "open"}

    monkeypatch.setattr(incidents.github, "_get", AsyncMock(side_effect=pr_state))
    await incidents.reconcile()
    rows = (await store.overview())["assessments"]
    assert rows[-1]["status"] == "failed"
    assert all(r["status"] == "pr_open" for r in rows[:-1])
    assert incidents.github._get.await_count == 201


async def test_repository_and_operator_action_conflicts_are_explicit(database):
    assert not await store.act(999, "resolve", "Verified deployment.", "admin")
    await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    with pytest.raises(ValueError, match="already monitored"):
        await store.enroll(RepositoryInput(repo_slug="EXAMPLE/App"), "admin")
    await store.ingest(incident_body(), "admin", manual=True)
    await incidents.ensure_assessments()
    row = (await incidents.due_assessments())[0]
    await incidents.queue_assessment(row)
    with pytest.raises(ValueError, match="Wait for"):
        await store.act(row["id"], "resolve", "Verified deployment.", "admin")


async def test_scan_import_keeps_unfixed_evidence_and_only_newer_observations(database):
    from datetime import datetime, timedelta, timezone

    repo = await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    body = security_scans.ScanInput.model_validate(
        {
            "repository_id": repo["id"],
            "image_digest": "sha256:" + "a" * 64,
            "scanned_at": datetime.now(timezone.utc),
            "report": {
                "Results": [
                    {
                        "Target": "server",
                        "Vulnerabilities": [
                            {
                                "VulnerabilityID": "CVE-2026-12345",
                                "PkgName": "parser",
                                "InstalledVersion": "1.0",
                                "Severity": "CRITICAL",
                            }
                        ],
                    }
                ]
            },
        }
    )
    result = await security_scans.ingest_scan(body, "admin")
    assert result["accepted"] == 1
    await incidents.ensure_assessments()
    row = (await incidents.due_assessments())[0]
    assert row["scans"][0]["findings"][0]["fixed_version"] is None
    await store.act(row["id"], "resolve", "No exposed runtime in this deployment.", "admin")
    await security_scans.ingest_scan(body, "admin")
    assert not await incidents.due_assessments()
    fresh = body.model_copy(update={"scanned_at": body.scanned_at + timedelta(seconds=1)})
    await security_scans.ingest_scan(fresh, "admin")
    assert (await incidents.due_assessments())[0]["incident_revision"] == row["incident_revision"] + 1
    with pytest.raises(ValueError, match="Register the repository"):
        await security_scans.ingest_scan(body.model_copy(update={"repository_id": 999}), "admin")


async def test_admin_api_updates_evidence_and_reports_action_conflicts(database):
    main.app.dependency_overrides[auth.current_principal] = lambda: auth.Principal(
        "admin-subject", "admin", frozenset({"logos_admin"})
    )
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/security")).json()["repositories"] == []
            assert (await client.post("/security/repositories", json={"repo_slug": "example/app"})).status_code == 201
            assert (await client.post("/security/repositories", json={"repo_slug": "example/app"})).status_code == 409
            original = await store.ingest(incident_body(), "feed:github")
            response = await client.post(
                "/security/incidents",
                json=incident_body(summary="The vendor confirmed another exposed server in the image.").model_dump(),
            )
            assert response.status_code == 201
            assert response.json()["revision"] == original["revision"] + 1
            assert response.json()["manual_override"]
            await incidents.ensure_assessments()
            row = (await incidents.due_assessments())[0]
            action = {"action": "resolve", "reason": "Verified the running deployment and configuration."}
            assert (await client.post("/security/assessments/999/actions", json=action)).status_code == 404
            assert (await client.post(f"/security/assessments/{row['id']}/actions", json=action)).json() == {"ok": True}
            history = (await client.get(f"/security/assessments/{row['id']}/events")).json()
            assert history[0]["created_by"] == "admin"
            assert history[0]["detail"]["revision"] == response.json()["revision"]
            await store.act(row["id"], "recheck", "Verify the repository again.", "admin")
            await incidents.queue_assessment((await incidents.due_assessments())[0])
            assert (await client.post(f"/security/assessments/{row['id']}/actions", json=action)).status_code == 409
            scan = {
                "repository_id": row["repository_id"],
                "image_digest": "sha256:" + "a" * 64,
                "scanned_at": "2026-01-01T00:00:00Z",
                "report": {"Results": []},
            }
            assert (await client.post("/security/scans/trivy", json=scan)).json()["accepted"] == 0
            assert (await client.post("/security/scans/trivy", json={**scan, "repository_id": 999})).status_code == 422
    finally:
        main.app.dependency_overrides.clear()


async def test_manual_precedence_migration_backfills_existing_records(database):
    async with database.begin() as conn:
        await conn.execute(text("ALTER TABLE security_incidents DROP COLUMN manual_override"))
        await conn.execute(text("""INSERT INTO security_incidents
            (advisory_id, title, severity, source_url, summary, importance, created_by)
            SELECT 'CVE-2026-' || n, 'Parser', 'critical', 'https://example.org', 'Details', 'Critical', creator
            FROM (VALUES (10000, 'admin'), (10001, 'feed:github'), (10002, 'scanner:trivy')) AS old(n, creator)"""))
        migration = Path(__file__).resolve().parents[2] / (
            "logos-webservice/src/main/resources/liquibase/changelog/079_security_manual_updates.xml"
        )
        sql = ET.parse(migration).getroot().find(".//{http://www.liquibase.org/xml/ns/dbchangelog}sql").text
        for statement in sql.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
        rows = (await conn.execute(text("SELECT created_by, manual_override FROM security_incidents"))).all()
        assert dict(rows) == {"admin": True, "feed:github": False, "scanner:trivy": False}


@pytest.mark.parametrize("creator,manual", [("scanner:trivy", False), ("feed:cisa", False), ("admin", True)])
async def test_later_github_evidence_enriches_incidents_without_replacing_display_fields(database, creator, manual):
    original = await store.ingest(incident_body(upstream_repositories=["vendor/server"]), creator, manual=manual)
    github_body = incident_body(title="A different feed title", summary="GitHub lists additional affected releases.")
    source = {
        "ghsa_id": "GHSA-2345-6789-abcd",
        "vulnerabilities": [{"first_patched_version": None}],
        "references": ["https://example.org/patch"],
    }
    merged = await store.ingest(github_body, "feed:github", source)
    for key in (
        "title",
        "summary",
        "importance",
        "source_url",
        "upstream_repositories",
        "created_by",
        "manual_override",
    ):
        assert merged[key] == original[key]
    assert merged["source_data"] == source
    assert merged["revision"] == original["revision"] + 1
    assert (await store.ingest(github_body, "feed:github", source))["revision"] == merged["revision"]
    patched = {**source, "vulnerabilities": [{"first_patched_version": "1.0.1"}]}
    updated = await store.ingest(github_body, "feed:github", patched)
    assert updated["source_data"]["vulnerabilities"] == patched["vulnerabilities"]
    assert updated["revision"] == merged["revision"] + 1
    exploited = await store.ingest(incident_body(known_exploited=True), "feed:cisa")
    assert exploited["known_exploited"]
    assert exploited["source_data"]["ghsa_id"] == source["ghsa_id"]


async def test_upstream_timeouts_preserve_missing_evidence_and_queue_later_assessments(database, monkeypatch):
    monkeypatch.setattr(incidents, "settings", replace(incidents.settings, security_feeds_enabled=False))
    monkeypatch.setattr(incidents.github, "_get", AsyncMock(side_effect=httpx.ReadTimeout("GitHub timeout")))
    await store.enroll(RepositoryInput(repo_slug="example/app"), "admin")
    for n in (12345, 12346):
        await store.ingest(
            incident_body(advisory_id=f"CVE-2026-{n}", upstream_repositories=["vendor/server"]),
            "feed:github",
            {"ghsa_id": "GHSA-2345-6789-abcd"},
        )
    await incidents.SecurityPoller().poll_once()
    async with database.begin() as conn:
        sessions = (await conn.execute(text("SELECT task, status FROM agent_sessions ORDER BY id"))).mappings().all()
        assert len(sessions) == 2
        for row in sessions:
            assert row["status"] == "queued"
            snapshot = json.loads(row["task"].split("Evidence snapshot:\n")[1])
            assert snapshot["upstream"] == [
                {"repository": "vendor/server", "error": "GitHub timeout"},
                {"advisory": "GHSA-2345-6789-abcd", "error": "GitHub timeout"},
            ]
