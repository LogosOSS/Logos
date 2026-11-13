"""GitHub operations stay with their session when repositories run concurrently."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app import github, repository_context, sessions


async def test_concurrent_tasks_keep_separate_github_targets(monkeypatch):
    seen = []

    async def get(path):
        await asyncio.sleep(0)
        seen.append(path)
        return {}

    async def read(slug):
        with repository_context.scope({"repo_slug": slug}):
            await github.pull_request(1)
            await asyncio.sleep(0)
            assert repository_context.slug("default/repo") == slug

    monkeypatch.setattr(github, "_get", get)
    await asyncio.gather(read("team/one"), read("team/two"))
    assert set(seen) == {"/repos/team/one/pulls/1", "/repos/team/two/pulls/1"}
    assert repository_context.current() is None


async def test_pending_reply_uses_repository_saved_on_session(monkeypatch):
    manager = sessions.SessionManager()
    row = {"id": 7, "repo_slug": "team/project", "reply_target": "issue:1"}
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value=row))
    seen = []

    async def post(session_id, session, target):
        seen.append((repository_context.slug("default/repo"), target))

    monkeypatch.setattr(manager, "_post_single_reply", post)
    await manager._post_reply_once(7)
    assert seen == [("team/project", "issue:1")]
    assert repository_context.current() is None


async def test_check_followup_uses_repository_saved_on_session(monkeypatch):
    manager = sessions.SessionManager()
    row = {"id": 7, "repo_slug": "team/project"}
    seen = []

    async def checks(sha):
        seen.append(repository_context.slug("default/repo"))
        return "success", "passed"

    monkeypatch.setattr(sessions.github, "wait_for_checks", checks)
    monkeypatch.setattr(manager, "_checks_settled", AsyncMock())
    await manager._take_up_a_red_build(row, "feature", "a" * 40)
    assert seen == ["team/project"]
    assert repository_context.current() is None


async def test_linked_repository_never_dispatches_logos_deploy(monkeypatch):
    manager = sessions.SessionManager()
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value={
        "id": 7, "repo_slug": "team/project", "deploy_to_dev": True,
    }))
    dispatch = AsyncMock()
    monkeypatch.setattr(sessions.github, "dispatch_dev_deploy", dispatch)
    assert await manager._maybe_deploy(7, {}) == (None, None)
    dispatch.assert_not_called()


def test_namespaced_reference_keeps_the_assigned_issue_number():
    assert repository_context.local_ref("repo:team/project:issue-42") == "issue-42"
    assert sessions._fallback_subject({"trigger_ref": "repo:team/project:issue-42"}) == "Work on issue #42"
