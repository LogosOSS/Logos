"""PostgreSQL regression of linked-repository sessions: fork destination, grants, workspace moves.

Run with SECURITY_TEST_DATABASE_URL=postgresql+asyncpg://... against a test DB.
Every run uses and removes its own schema; application data is untouched.
"""

import asyncio
import os
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app import analysis_triggers, controls, db, inference_settings, model_policy, repository_context, triggers
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Taken before the shared fixture stubs it for the tests without a database.
REAL_SESSION_MEMORY_MB = db.session_memory_mb
REAL_INFERENCE_SELECTION = db.agent_inference_selection
REAL_ENABLED_CODING = db.enabled_coding_repositories
REAL_NEXT_QUEUED = db.next_queued_session
REAL_SESSIONS_IN_STATUS = db.sessions_in_status
REAL_OWING_REPLACEMENT = db.sessions_owing_a_replacement

pytestmark = pytest.mark.skipif(
    not os.getenv("SECURITY_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set"
)


@pytest.fixture
async def database(monkeypatch):
    url = os.environ["SECURITY_TEST_DATABASE_URL"]
    schema = "sandbox_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    factory = async_sessionmaker(engine)
    monkeypatch.setattr(db, "sessionmaker", lambda: factory)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE teams (id SERIAL PRIMARY KEY, name text)"))
            await conn.execute(text("""CREATE TABLE team_repositories (
                id SERIAL PRIMARY KEY, team_id integer, repo_url text, repo_slug text, branch text, paths jsonb,
                automatic_workflow_analysis boolean NOT NULL DEFAULT TRUE,
                agent_enabled boolean NOT NULL DEFAULT TRUE, agent_instructions text, verification_command text,
                agent_egress_hosts jsonb NOT NULL DEFAULT '[]'::jsonb,
                agent_egress_unrestricted boolean NOT NULL DEFAULT FALSE,
                agent_may_comment boolean NOT NULL DEFAULT FALSE)"""))
            await conn.execute(text("""CREATE TABLE agent_workspaces (
                id SERIAL PRIMARY KEY, name text UNIQUE, base_branch text, volume_name text,
                created_by text, created_at timestamptz DEFAULT now(), ephemeral boolean DEFAULT FALSE,
                archived_at timestamptz)"""))
            await conn.execute(text("""CREATE TABLE agent_sessions (
                id SERIAL PRIMARY KEY, workspace_id integer REFERENCES agent_workspaces(id),
                task text, model text, status text, created_by text, created_at timestamptz DEFAULT now(),
                inference_provider_id integer, inference_model_id integer,
                started_at timestamptz, finished_at timestamptz, exit_code integer, error text,
                container_id text, branch_name text, pr_url text, open_pull_request boolean,
                deploy_to_dev boolean, screenshot_paths jsonb, no_push boolean DEFAULT FALSE,
                trigger_kind text, trigger_ref text, reply_target text, reaction_target text,
                priority integer, priority_reason text, environment_notes text,
                repo_url text, repo_slug text, team_repository_id integer, verification_command text,
                repo_base_branch text, push_repo_slug text, egress_token_hash text,
                memory_mb integer, memory_override_mb integer, memory_peak_bytes bigint, subagent_count integer,
                progress_percent integer, progress_active_s bigint, progress_reported_at timestamptz,
                active_seconds double precision NOT NULL DEFAULT 0, active_since timestamptz,
                tokens_in bigint, tokens_out bigint, cost_usd numeric)"""))
            await conn.execute(text("""CREATE TABLE agent_inference_settings (
                id integer PRIMARY KEY, mode text, model text, provider_id integer, model_id integer)"""))
            await conn.execute(text("""CREATE TABLE ai_workflow_analyses (
                id SERIAL PRIMARY KEY, team_id integer, team_repository_id integer, status text, source text,
                commit_sha text, error text, started_at timestamptz, finished_at timestamptz,
                automatically_queued boolean, agent_session_id integer)"""))
            await conn.execute(text("""CREATE UNIQUE INDEX one_pending_analysis
                ON ai_workflow_analyses(team_repository_id)
                WHERE status IN ('queued','running')"""))
            await conn.execute(text("CREATE TABLE agent_events (id SERIAL PRIMARY KEY, session_id integer, kind text)"))
            await conn.execute(text("""INSERT INTO team_repositories (team_id, repo_url, repo_slug, branch,
                agent_egress_hosts, agent_may_comment)
                VALUES (1, 'https://github.com/o/a', 'o/a', 'main', '["github.com"]', TRUE)"""))
            await conn.execute(text("""INSERT INTO agent_workspaces (name, base_branch, volume_name, created_by)
                VALUES ('ws', 'main', 'v', 'op')"""))
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


def _session(**overrides):
    values = dict(
        workspace_id=1,
        task="t",
        model=None,
        created_by="op",
        open_pull_request=True,
        deploy_to_dev=False,
        screenshot_paths=[],
        repo_url="https://github.com/o/a",
        repo_slug="o/a",
        team_repository_id=1,
        repo_base_branch="main",
    )
    values.update(overrides)
    return values


async def test_the_fork_recorded_at_preparation_reaches_finalization(database):
    session_id = await db.create_session(**_session())
    await db.update_session(session_id, push_repo_slug="LogosOSSAgent/a")

    reloaded = await db.get_session(session_id)

    assert reloaded["push_repo_slug"] == "LogosOSSAgent/a"


async def test_the_published_base_fork_is_recovered_across_private_sessions(database):
    base = "logos/agent/ws/session-1"
    publisher = await db.create_session(**_session(branch=base))
    await db.update_session(publisher, status="succeeded", push_repo_slug="agent/a")
    private = await db.create_session(
        **_session(branch="logos/agent/ws/session-2", no_push=True, open_pull_request=False)
    )
    await db.update_session(private, status="succeeded")
    other_branch = await db.create_session(**_session(branch="logos/agent/ws/session-3"))
    await db.update_session(other_branch, status="succeeded", push_repo_slug="agent/latest")
    failed = await db.create_session(**_session(branch=base))
    await db.update_session(failed, status="failed", push_repo_slug="agent/failed")

    assert await db.published_checkout_fork(1, "O/A", base) == "agent/a"
    assert await db.published_checkout_fork(1, "o/a", base, has_publication_receipt=True) == "agent/failed"
    assert await db.published_checkout_fork(1, "o/b", base) is None
    assert await db.published_checkout_fork(2, "o/a", base) is None
    assert await db.published_checkout_fork(1, "o/a", "logos/agent/ws/session-2") is None


async def test_grants_follow_the_link_only_while_it_names_the_sessions_repository(database):
    session_id = await db.create_session(**_session())
    await db.update_session(session_id, status="running", egress_token_hash="h")

    assert (await db.sandbox_permissions(1, "o/a"))["agent_may_comment"] is True
    assert (await db.egress_grant(session_id))["hosts"] == ["github.com"]

    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET repo_slug = 'o/b' WHERE id = 1"))

    assert await db.sandbox_permissions(1, "o/a") == {
        "agent_egress_hosts": [],
        "agent_egress_unrestricted": False,
        "agent_may_comment": False,
    }
    assert (await db.egress_grant(session_id))["hosts"] == []


async def _seed_provider_privacy(database):
    path = Path(__file__).resolve().parents[2] / "logos-webservice/src/main/resources/liquibase/changelog"
    migration = (path / "095_team_privacy.xml").read_text()
    async with database.begin() as conn:
        await conn.execute(text("ALTER TABLE teams ADD COLUMN privacy_scope text"))
        await conn.execute(text("""CREATE TABLE providers (
            id integer, provider_type text, privacy_level text, eu_processing_verified boolean, base_url text,
            cloud_provider_type text, name text, auth_name text, auth_format text, api_key text)"""))
        await conn.execute(text("CREATE TABLE models (id integer, name text)"))
        await conn.execute(
            text("CREATE TABLE model_provider (provider_id integer, endpoint text, model_id integer, api_key text)")
        )
        await conn.execute(text("INSERT INTO teams VALUES (1, 'team', 'WORLDWIDE')"))
        await conn.execute(text("""INSERT INTO providers VALUES
            (3, 'cloud', 'CLOUD_NOT_IN_EU_BY_US_PROVIDER', FALSE, 'https://cloud.example',
                 'logos', 'Synthetic Logos', NULL, NULL, 'fake-key'),
            (4, 'cloud', 'CLOUD_IN_EU_BY_EU_PROVIDER', TRUE, 'https://cloud-eu.example',
                 'logos', 'Synthetic Logos EU', NULL, NULL, 'fake-key')"""))
        await conn.execute(text("INSERT INTO models VALUES (8, 'cloud-model')"))
        await conn.execute(text("INSERT INTO model_provider VALUES (3, NULL, 8, NULL), (4, NULL, 8, NULL)"))
        for name in ("provider_processing_privacy", "team_privacy_providers"):
            statement = f"CREATE VIEW {name}" + migration.split(f"CREATE VIEW {name}", 1)[1].split(";", 1)[0]
            await conn.execute(text(statement))


async def test_remote_agent_applies_live_team_privacy_view_and_never_upgrades_a_deleted_link_to_platform(database):
    await _seed_provider_privacy(database)
    assert await db.agent_cloud_allowed_for_repository(3, None, None)
    assert not await db.agent_cloud_allowed_for_repository(3, None, "o/a")
    assert not await db.agent_cloud_allowed_for_repository(3, 1, None)
    assert not await db.agent_cloud_allowed_for_repository(3, 1, "o/other")
    assert await db.agent_cloud_allowed_for_repository(3, 1, "O/A")
    async with database.begin() as conn:
        await conn.execute(text("UPDATE teams SET privacy_scope = 'EU' WHERE id = 1"))

    assert not await db.agent_cloud_allowed_for_repository(3, 1, "o/a")
    assert await db.agent_cloud_allowed_for_repository(4, 1, "o/a")

    async with database.begin() as conn:
        await conn.execute(text("UPDATE teams SET privacy_scope = 'LOCAL' WHERE id = 1"))
    assert not await db.agent_cloud_allowed_for_repository(4, 1, "o/a")

    async with database.begin() as conn:
        await conn.execute(text("UPDATE teams SET privacy_scope = 'WORLDWIDE' WHERE id = 1"))
        await conn.execute(text("UPDATE team_repositories SET agent_enabled = FALSE WHERE id = 1"))
    assert not await db.agent_cloud_allowed_for_repository(3, 1, "o/a")
    poller = analysis_triggers.AnalysisPoller()
    (link,) = await poller._links_needing_analysis()
    automatic_id = await poller._queue(link)
    assert automatic_id is not None

    # Coding is disabled, but the linked read-only analysis is independent.
    async def permitted(session_id):
        row = await db.get_session(session_id)
        return await db.agent_cloud_allowed_for_repository(
            3,
            row["team_repository_id"],
            row["repo_slug"],
            workflow_analysis=True,
            session_id=session_id,
        )

    assert await permitted(automatic_id)
    assert not await db.agent_cloud_allowed_for_repository(3, None, None, workflow_analysis=True)
    assert not await db.agent_cloud_allowed_for_repository(3, 1, "o/a", workflow_analysis=True, session_id=99)
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = FALSE"))
        await conn.execute(text("UPDATE ai_workflow_analyses SET status = 'failed'"))
    assert not await permitted(automatic_id)
    manual_id = await db.create_session(**_session(trigger_kind="analysis", no_push=True), automatic_analysis=False)
    assert await permitted(manual_id)
    async with database.begin() as conn:
        await conn.execute(text("UPDATE agent_sessions SET no_push = FALSE WHERE id = :id"), {"id": manual_id})
    assert not await permitted(manual_id)
    async with database.begin() as conn:
        await conn.execute(text("UPDATE agent_sessions SET no_push = TRUE WHERE id = :id"), {"id": manual_id})
        await conn.execute(text("UPDATE teams SET privacy_scope = 'LOCAL'"))
    assert not await permitted(manual_id)
    async with database.begin() as conn:
        await conn.execute(text("DELETE FROM team_repositories WHERE id = 1"))
    assert not await permitted(manual_id)
    assert not await db.agent_cloud_allowed_for_repository(3, 1, "o/a")
    assert not await db.agent_cloud_allowed_for_repository(3, None, "o/a")


@pytest.mark.parametrize(
    "scenario,expected_kinds",
    [
        ("cloud-only", {"issue", "analysis"}),
        ("platform-cloud-only", {"issue", "analysis"}),
        ("cloud-nightly", {"issue", "analysis"}),
        ("legacy-local", {"issue", "analysis"}),
        ("pinned-local", {"issue", "analysis"}),
        ("no-local", set()),
        ("unapproved-cloud", set()),
        ("wrong-model", set()),
        ("invalid-credential", set()),
        ("privacy-local", set()),
        ("analysis-opt-out", {"issue"}),
        ("coding-opt-out", {"analysis"}),
        ("settings-changed", set()),
    ],
)
async def test_automatic_pollers_admit_only_the_selected_permitted_inference(
    database, monkeypatch, scenario, expected_kinds
):
    await _seed_provider_privacy(database)
    monkeypatch.setattr(db, "agent_inference_selection", REAL_INFERENCE_SELECTION)
    monkeypatch.setattr(db, "enabled_coding_repositories", REAL_ENABLED_CODING)
    configured = ("https://cloud-eu.example",) if scenario == "unapproved-cloud" else ("https://cloud.example",)
    monkeypatch.setattr(
        inference_settings, "settings", replace(inference_settings.settings, remote_allowed_origins=configured)
    )
    monkeypatch.setattr(
        analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=-1)
    )
    local_available = scenario in {
        "legacy-local",
        "pinned-local",
        "unapproved-cloud",
        "wrong-model",
        "invalid-credential",
    }
    if not local_available:
        monkeypatch.setattr(model_policy, "_current", model_policy.ModelPolicy(detail="No local models"))
    monkeypatch.setattr(controls, "current", AsyncMock(return_value=controls.Controls()))
    async with database.begin() as conn:
        if scenario == "pinned-local":
            await conn.execute(
                text("INSERT INTO agent_inference_settings VALUES (1, 'local', 'local-model', NULL, NULL)")
            )
        elif scenario not in {"legacy-local", "no-local"}:
            model = "wrong" if scenario == "wrong-model" else "cloud-model"
            await conn.execute(
                text("INSERT INTO agent_inference_settings VALUES (1, 'cloud', :model, 3, 8)"), {"model": model}
            )
        if scenario == "cloud-nightly":
            await conn.execute(text("""
                INSERT INTO ai_workflow_analyses (team_id, team_repository_id, status, commit_sha)
                VALUES (1, 1, 'succeeded', 'old-head')
                """))
        if scenario == "invalid-credential":
            await conn.execute(text("UPDATE providers SET auth_name = 'x-api-key' WHERE id = 3"))
        if scenario == "privacy-local":
            await conn.execute(text("UPDATE teams SET privacy_scope = 'LOCAL'"))
        if scenario == "analysis-opt-out":
            await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = FALSE"))
        if scenario == "coding-opt-out":
            await conn.execute(text("UPDATE team_repositories SET agent_enabled = FALSE"))

    coding = triggers.TriggerPoller()
    analysis = analysis_triggers.AnalysisPoller()

    async def candidates(_now):
        platform = repository_context.current() is None
        if platform != (scenario == "platform-cloud-only"):
            return []
        return [{"ref": "issue-812", "task": "Synthetic assigned issue", "kind": "issue"}]

    # Only GitHub discovery/notification is simulated. Workspaces, the saved
    # selection, privacy views, and queue writes all use PostgreSQL.
    monkeypatch.setattr(coding, "_candidates", candidates)
    monkeypatch.setattr(coding, "_mark_comments_scanned", AsyncMock())
    monkeypatch.setattr(coding, "_acknowledge", AsyncMock())
    if scenario == "cloud-nightly":
        monkeypatch.setattr(analysis, "_nightly_due", lambda _now: True)
        monkeypatch.setattr(analysis_triggers.github, "branch_head", AsyncMock(return_value="new-head"))
    changed_after_preflight = []
    if scenario == "settings-changed":
        free_workspace = coding._free_workspace
        ensure_workspace = analysis._ensure_workspace

        async def change_selection():
            async with database.begin() as conn:
                await conn.execute(
                    text("UPDATE agent_inference_settings SET model = 'other-model', provider_id = 4, model_id = 9")
                )

        async def original_selection():
            async with database.begin() as conn:
                await conn.execute(
                    text("UPDATE agent_inference_settings SET model = 'cloud-model', provider_id = 3, model_id = 8")
                )

        async def change_after_free(**kwargs):
            workspace = await free_workspace(**kwargs)
            await change_selection()
            changed_after_preflight.append("coding")
            return workspace

        async def change_after_ensure(*args):
            workspace = await ensure_workspace(*args)
            await change_selection()
            changed_after_preflight.append("analysis")
            return workspace

        monkeypatch.setattr(coding, "_free_workspace", change_after_free)
        monkeypatch.setattr(analysis, "_ensure_workspace", change_after_ensure)

    coding_ids = await coding.poll_once()
    if scenario == "settings-changed":
        await original_selection()
    analysis_ids = await analysis.poll_once()
    if scenario == "settings-changed":
        assert changed_after_preflight == ["coding", "analysis"]
    rows = await db.list_sessions()
    assert {row["trigger_kind"] for row in rows} == expected_kinds, coding.status()["last_error"]
    assert set(coding_ids + analysis_ids) == {row["id"] for row in rows}
    assert coding._acknowledge.await_count == len(coding_ids)
    for row in rows:
        pin = row["model"], row["inference_provider_id"], row["inference_model_id"]
        if scenario == "legacy-local":
            assert pin == (None, None, None)
        elif scenario == "pinned-local":
            assert pin == ("local-model", None, None)
        else:
            assert pin == ("cloud-model", 3, 8)
        platform_issue = scenario == "platform-cloud-only" and row["trigger_kind"] == "issue"
        assert row["team_repository_id"] == (None if platform_issue else 1)
        assert row["status"] == "queued"
        if row["trigger_kind"] == "analysis":
            assert row["no_push"]
            async with database.begin() as conn:
                assert await conn.scalar(
                    text("SELECT automatically_queued FROM ai_workflow_analyses WHERE agent_session_id = :id"),
                    {"id": row["id"]},
                )


async def test_unrestricted_access_follows_the_link_only_while_it_names_the_sessions_repository(database):
    session_id = await db.create_session(**_session())
    await db.update_session(session_id, status="running", egress_token_hash="h")
    assert (await db.egress_grant(session_id))["unrestricted"] is False

    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET agent_egress_unrestricted = TRUE WHERE id = 1"))

    assert (await db.sandbox_permissions(1, "o/a"))["agent_egress_unrestricted"] is True
    assert (await db.egress_grant(session_id))["unrestricted"] is True

    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET repo_slug = 'o/b' WHERE id = 1"))

    assert (await db.sandbox_permissions(1, "o/a"))["agent_egress_unrestricted"] is False
    assert (await db.egress_grant(session_id))["unrestricted"] is False


async def test_an_idle_workspace_moves_to_the_new_repositorys_branch(database):
    await db.create_session(**_session(repo_slug=None, repo_url=None, team_repository_id=None))
    async with database.begin() as conn:
        await conn.execute(text("UPDATE agent_sessions SET status = 'succeeded'"))

    await db.create_session(**_session(repo_base_branch="develop"), point_workspace_at="develop")

    assert (await db.get_workspace(1))["base_branch"] == "develop"


async def test_two_requests_for_different_repositories_cannot_share_an_idle_workspace(database):
    first = db.create_session(**_session(), point_workspace_at="main")
    second = db.create_session(
        **_session(repo_slug=None, repo_url=None, team_repository_id=None), point_workspace_at="trunk"
    )

    results = await asyncio.gather(first, second, return_exceptions=True)

    assert sum(isinstance(result, int) for result in results) == 1
    assert sum(isinstance(result, db.WorkspaceBusy) for result in results) == 1


async def test_a_retry_cannot_join_a_workspace_busy_with_another_repository(database):
    await db.create_session(**_session(repo_slug="o/b", repo_url="https://github.com/o/b"))

    with pytest.raises(db.WorkspaceBusy):
        await db.create_session(**_session())


async def test_reserved_memory_counts_sessions_that_hold_a_container(database):
    holding = []
    for status in ("starting", "running", "paused", "finalizing", "queued", "succeeded"):
        session_id = await db.create_session(**_session(repo_slug=None, repo_url=None, team_repository_id=None))
        async with database.begin() as conn:
            await conn.execute(
                text("UPDATE agent_sessions SET status = :s WHERE id = :i"), {"s": status, "i": session_id}
            )
        holding.append(session_id)
    await db.update_session(holding[0], memory_mb=2048)

    # starting 2048 + running, paused and finalizing at the default of 1024.
    assert await db.reserved_memory_mb(1024) == 2048 + 3 * 1024


async def _queued_job(database, *, automatic=False, priority=50, branch=None):
    async with database.begin() as conn:
        workspace = (
            await conn.execute(
                text("INSERT INTO agent_workspaces (name) VALUES (:name) RETURNING id"), {"name": uuid.uuid4().hex}
            )
        ).scalar_one()
        return (
            await conn.execute(
                text("""
                    INSERT INTO agent_sessions (workspace_id, status, trigger_ref, priority, branch_name)
                    VALUES (:workspace, 'queued', :trigger, :priority, :branch) RETURNING id
                    """),
                {
                    "workspace": workspace,
                    "trigger": uuid.uuid4().hex if automatic else None,
                    "priority": priority,
                    "branch": branch,
                },
            )
        ).scalar_one()


@pytest.mark.parametrize("ceiling", [0, 1, 2])
async def test_automatic_work_can_fill_but_not_exceed_the_shared_ceiling(database, ceiling):
    queued = [await _queued_job(database, automatic=True) for _ in range(3)]

    results = await asyncio.gather(*(db.claim_session(job, max_active=ceiling) for job in queued))

    assert sum(row is not None for row in results) == ceiling
    assert len(await REAL_SESSIONS_IN_STATUS(db.SessionStatus.STARTING)) == ceiling


async def test_a_manual_arrival_wins_the_next_free_slot_without_displacing_running_work(database):
    first = await _queued_job(database, automatic=True, priority=100)
    assert await db.claim_session(first, max_active=2)
    await db.update_session(first, status="running")
    automated = await _queued_job(database, automatic=True, priority=100)
    assert (await REAL_NEXT_QUEUED())["id"] == automated

    manual = await _queued_job(database, priority=1)
    assert await db.claim_session(automated, max_active=2) is None
    assert (await REAL_NEXT_QUEUED())["id"] == manual
    assert await db.claim_session(manual, max_active=2)
    assert await db.claim_session(automated, max_active=2) is None
    assert (await db.get_session(first))["status"] == "running"
    assert (await db.get_session(automated))["status"] == "queued"


async def test_a_branch_blocked_manual_job_does_not_keep_the_other_slot_idle(database):
    first = await _queued_job(database, automatic=True, branch="shared")
    assert await db.claim_session(first, max_active=2)
    await db.update_session(first, status="running")
    manual = await _queued_job(database, branch="shared")
    automated = await _queued_job(database, automatic=True, branch="independent")

    assert (await REAL_NEXT_QUEUED())["id"] == automated
    assert await db.claim_session(automated, max_active=2)
    assert (await db.get_session(manual))["status"] == "queued"


async def test_the_claim_records_the_memory_of_the_session(database):
    session_id = await db.create_session(**_session(repo_slug=None, repo_url=None, team_repository_id=None))

    claimed = await db.claim_session(session_id, max_active=1, memory_mb=2048)

    assert claimed["status"] == "starting"
    assert claimed["memory_mb"] == 2048
    assert await REAL_SESSION_MEMORY_MB(session_id) == 2048
    assert await db.reserved_memory_mb(4096) == 2048


async def test_manual_memory_override_survives_queuing_and_counts_only_at_claim(database):
    manual = await db.create_session(**_session(memory_override_mb=6144))
    queued = await REAL_NEXT_QUEUED()
    assert queued["id"] == manual
    assert queued["memory_override_mb"] == 6144
    assert (await db.get_session(manual))["memory_mb"] is None
    assert await db.reserved_memory_mb(1024) == 0

    claimed = await db.claim_session(manual, max_active=2, memory_mb=queued["memory_override_mb"])
    assert claimed["memory_mb"] == claimed["memory_override_mb"] == 6144
    assert await db.reserved_memory_mb(1024) == 6144


async def test_claims_in_two_transactions_cannot_overcommit_the_memory_budget(database):
    manual1, manual2 = await _queued_job(database), await _queued_job(database)
    limits = dict(max_active=2, memory_mb=6144, memory_budget_mb=8192, legacy_default_mb=4096)
    claims = await asyncio.gather(db.claim_session(manual1, **limits), db.claim_session(manual2, **limits))
    assert sum(claim is not None for claim in claims) == 1
    assert await db.reserved_memory_mb(4096) == 6144
    deferred = manual1 if claims[0] is None else manual2
    assert (await db.get_session(deferred))["status"] == "queued"
    assert await db.claim_session(deferred, **{**limits, "memory_mb": 2048})
    assert await db.reserved_memory_mb(4096) == 8192


async def test_a_cancellation_can_only_transfer_the_reservation_it_read(database):
    session_id = await db.create_session(**_session(repo_slug=None, repo_url=None, team_repository_id=None))
    queued = await db.get_session(session_id)
    await db.claim_session(session_id, memory_mb=4096)

    assert not await db.transition_session(
        session_id, db.SessionStatus.CANCELLED, expected_status=db.SessionStatus(queued["status"])
    )
    assert await db.reserved_memory_mb(1024) == 4096
    assert await db.transition_session(
        session_id, db.SessionStatus.CANCELLED, expected_status=db.SessionStatus.STARTING
    )
    assert await db.reserved_memory_mb(1024) == 0


async def test_peak_is_nullable_and_only_increases_across_sessions_and_status_changes(database):
    session_id = await db.create_session(**_session())
    assert (await db.get_session(session_id))["memory_peak_bytes"] is None
    await asyncio.gather(db.record_memory_peaks({session_id: 2**32}), db.record_memory_peaks({session_id: 100}))
    await db.update_session(session_id, status="succeeded")
    assert (await db.list_sessions())[0]["memory_peak_bytes"] == 2**32


async def test_automatic_analysis_rechecks_opt_out_after_selection_and_before_attach(database):
    poller = analysis_triggers.AnalysisPoller()
    (link,) = await poller._links_needing_analysis()
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = FALSE WHERE id = 1"))
    with pytest.raises(ValueError, match="automatic workflow analysis"):
        await db.create_session(**_session(), automatic_analysis=True)
    assert await db.list_sessions() == []
    assert await poller._queue(link) is None
    assert await poller._links_needing_analysis() == []
    async with database.begin() as conn:
        await conn.execute(
            text("INSERT INTO ai_workflow_analyses (team_repository_id, status) VALUES (1, 'succeeded')")
        )
    assert await poller._links_for_nightly() == []
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = TRUE WHERE id = 1"))
    (nightly,) = await poller._links_for_nightly()
    session_id = await poller._queue(nightly)
    assert session_id is not None
    async with database.begin() as conn:
        assert (
            await conn.scalar(text("SELECT agent_session_id FROM ai_workflow_analyses WHERE automatically_queued"))
            == session_id
        )


async def test_failed_analysis_retry_honors_opt_out_and_attaches_cancellable_analysis(database, monkeypatch):
    from unittest.mock import AsyncMock

    from app.sessions import SessionManager

    manager = SessionManager()
    monkeypatch.setattr(manager, "request_scheduler_pass", lambda: None)
    monkeypatch.setattr(db, "add_event", AsyncMock())
    poller = analysis_triggers.AnalysisPoller()
    (link,) = await poller._links_needing_analysis()
    parent_id = await poller._queue(link)
    await db.update_session(parent_id, status="failed")
    parent = await db.get_session(parent_id)
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = FALSE"))
    assert await manager.take_up_again(parent) is None
    assert len(await db.list_sessions()) == 1
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = TRUE"))
    retry = await manager.take_up_again(parent)
    assert retry and retry != parent_id
    assert await poller._queue(link) is None
    async with database.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT agent_session_id, status, automatically_queued " "FROM ai_workflow_analyses ORDER BY id")
            )
        ).all()
        assert rows == [(parent_id, "failed", True), (retry, "queued", True)]


@pytest.mark.parametrize("automatic", [True, False])
async def test_recovery_preserves_original_analysis_provenance_while_opted_out(database, monkeypatch, automatic):
    from datetime import datetime, timedelta, timezone
    from unittest.mock import AsyncMock

    from app.sessions import SessionManager

    now = datetime.now(timezone.utc)
    parent = await db.create_session(
        **_session(trigger_kind="analysis", trigger_ref="team-repository:1"), automatic_analysis=automatic
    )
    await db.update_session(parent, status="failed", finished_at=now)
    async with database.begin() as conn:
        await conn.execute(text("UPDATE team_repositories SET automatic_workflow_analysis = FALSE"))
    manager = SessionManager()
    monkeypatch.setattr(manager, "request_scheduler_pass", lambda: None)
    monkeypatch.setattr(db, "add_event", AsyncMock())
    pending = await REAL_OWING_REPLACEMENT(max_attempts=3, since=now - timedelta(hours=1))
    assert [row["id"] for row in pending] == ([] if automatic else [parent])
    retry = await manager.take_up_again(await db.get_session(parent))
    if automatic:
        assert retry is None
    else:
        assert retry is not None
        async with database.begin() as conn:
            assert (
                await conn.scalar(
                    text("SELECT automatically_queued FROM ai_workflow_analyses " "WHERE agent_session_id = :sid"),
                    {"sid": retry},
                )
                is False
            )


async def test_activity_clock_freezes_and_metadata_survives_reread_and_out_of_order_writes(
    database, monkeypatch, tmp_path
):
    from app import sessions

    monkeypatch.setattr(sessions, "artifact_dir", lambda _: tmp_path)
    sid = await db.create_session(**_session())
    await db.claim_session(sid, memory_mb=2048)
    await db.transition_session(sid, db.SessionStatus.RUNNING)
    async with database.begin() as conn:
        await conn.execute(
            text("UPDATE agent_sessions SET active_since = now() - interval '20 minutes' WHERE id = :id"), {"id": sid}
        )
    await db.transition_session(sid, db.SessionStatus.PAUSED)
    paused = await db.get_session(sid)
    assert 1199 < paused["active_seconds"] < 1202 and paused["active_since"] is None
    import json
    import time

    stamp = time.time_ns() // 1000
    path = tmp_path / "activity.json"
    path.write_text(
        json.dumps(dict(subagent_count=2, progress_percent=40, progress_active_s=1200, progress_reported_us=stamp))
    )
    await sessions.SessionManager()._collect_activity(sid)
    path.write_text(
        json.dumps(dict(subagent_count=1, progress_percent=20, progress_active_s=600, progress_reported_us=stamp - 100))
    )
    await sessions.SessionManager()._collect_activity(sid)
    await db.transition_session(sid, db.SessionStatus.RUNNING)
    assert (await db.get_session(sid))["active_since"]
    await db.transition_session(sid, db.SessionStatus.FINALIZING)
    await db.transition_session(sid, db.SessionStatus.SUCCEEDED)
    row = (await db.list_sessions())[0]
    assert (row["subagent_count"], row["progress_percent"], row["progress_active_s"]) == (2, 40, 1200)
    assert row["active_since"] is None
