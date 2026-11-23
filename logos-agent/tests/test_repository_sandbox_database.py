"""PostgreSQL regression of linked-repository sessions: fork destination, grants, workspace moves.

Run with SECURITY_TEST_DATABASE_URL=postgresql+asyncpg://... against a test DB.
Every run uses and removes its own schema; application data is untouched.
"""

import asyncio
import os
import uuid

import pytest
from app import db
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Taken before the shared fixture stubs it for the tests without a database.
REAL_SESSION_MEMORY_MB = db.session_memory_mb

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
                id SERIAL PRIMARY KEY, team_id integer, repo_url text, repo_slug text, branch text,
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
                started_at timestamptz, finished_at timestamptz, exit_code integer, error text,
                container_id text, branch_name text, pr_url text, open_pull_request boolean,
                deploy_to_dev boolean, screenshot_paths jsonb, no_push boolean DEFAULT FALSE,
                trigger_kind text, trigger_ref text, reply_target text, reaction_target text,
                priority integer, priority_reason text, environment_notes text,
                repo_url text, repo_slug text, team_repository_id integer, verification_command text,
                repo_base_branch text, push_repo_slug text, egress_token_hash text, memory_mb integer,
                tokens_in bigint, tokens_out bigint, cost_usd numeric)"""))
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


async def test_the_claim_records_the_memory_of_the_session(database):
    session_id = await db.create_session(**_session(repo_slug=None, repo_url=None, team_repository_id=None))

    claimed = await db.claim_session(session_id, trigger_quota=1, memory_mb=2048)

    assert claimed["status"] == "starting"
    assert claimed["memory_mb"] == 2048
    assert await REAL_SESSION_MEMORY_MB(session_id) == 2048
    assert await db.reserved_memory_mb(4096) == 2048


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
