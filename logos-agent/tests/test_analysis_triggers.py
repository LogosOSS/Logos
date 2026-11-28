"""Nightly re-analysis of linked team repositories."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app import analysis_triggers, github
from app.analysis_cadence import link_scope
from app.analysis_triggers import AnalysisPoller
from app.config import Settings

HEAD = "a" * 40
MOVED = "b" * 40
TAG = "c" * 40


def _link(link_id: int, last_commit: str | None) -> dict:
    link = {
        "id": link_id,
        "team_id": 5,
        "repo_url": f"https://github.com/acme/r{link_id}.git",
        "repo_slug": f"acme/r{link_id}",
        "branch": "main",
        "paths": None,
        "last_commit": last_commit,
        "last_success_at": datetime.now(timezone.utc) - timedelta(days=31),
    }
    link["workflow_release_baseline"] = {"scope": link_scope(link), "release_ids": []}
    return link


def _poller(monkeypatch, links: list[dict], heads: dict[str, str | None]) -> tuple[AnalysisPoller, list]:
    poller = AnalysisPoller()
    queued: list[tuple[str, str | None]] = []

    async def links_for_nightly():
        return links

    async def queue(link, *, target=None):
        queued.append((link["id"], target["sha"] if target else None))
        return 1000 + int(link["id"])

    async def branch_head(slug, branch):
        return heads.get(slug)

    monkeypatch.setattr(poller, "_links_for_nightly", links_for_nightly)
    monkeypatch.setattr(poller, "_queue", queue)
    monkeypatch.setattr(github, "branch_head", branch_head)
    monkeypatch.setattr(github, "stable_releases", AsyncMock(return_value=[]))
    monkeypatch.setattr(poller, "_seed_release_baseline", AsyncMock())
    monkeypatch.setattr(poller, "_record_release_target", AsyncMock(side_effect=lambda link, scope, release, sha: sha))
    return poller, queued


async def test_nightly_skips_unchanged_and_queues_moved_heads(monkeypatch):
    poller, queued = _poller(
        monkeypatch,
        [_link(1, HEAD), _link(2, HEAD), _link(3, HEAD)],
        {"acme/r1": HEAD, "acme/r2": MOVED, "acme/r3": None},
    )
    sessions = await poller._nightly_pass()
    assert queued == [(2, MOVED)]
    assert sessions == [1002]
    assert poller.status()["skipped_unchanged_total"] == 1


def test_nightly_runs_once_in_its_hour(monkeypatch):
    monkeypatch.setattr(analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=1))
    poller = AnalysisPoller()
    night = datetime(2026, 10, 3, 1, 5, tzinfo=timezone.utc)
    assert poller._nightly_due(night)
    poller._nightly_done_for = night.date()
    assert not poller._nightly_due(night.replace(minute=50))
    assert not poller._nightly_due(datetime(2026, 10, 3, 14, 0, tzinfo=timezone.utc))
    assert poller._nightly_due(datetime(2026, 10, 4, 1, 0, tzinfo=timezone.utc))


def test_nightly_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(
        analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=-1)
    )
    assert not AnalysisPoller()._nightly_due(datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc))


async def test_release_cutover_seeds_legacy_ids_without_replaying_them(monkeypatch):
    link = _link(1, HEAD)
    link["last_success_at"] = datetime.now(timezone.utc)
    link.pop("workflow_release_baseline")
    poller, queued = _poller(monkeypatch, [link], {link["repo_slug"]: MOVED})
    github.stable_releases.return_value = [github.StableRelease(7, "v1", datetime.now(timezone.utc))]

    assert await poller._nightly_pass() == []
    poller._seed_release_baseline.assert_awaited_once_with(link, link_scope(link), [7])
    assert queued == []


async def test_new_stable_tag_uses_its_commit_not_branch_tip_per_link(monkeypatch):
    links = [_link(1, HEAD), _link(2, HEAD)]
    for link in links:
        link.update(
            repo_slug="acme/shared",
            branch="develop",
            paths=[f"path-{link['id']}"],
            last_success_at=datetime.now(timezone.utc),
        )
        link["workflow_release_baseline"] = {"scope": link_scope(link), "release_ids": [7]}
    poller, queued = _poller(monkeypatch, links, {"acme/shared": MOVED})
    github.stable_releases.return_value = [
        # An existing prerelease may have an old publication timestamp after promotion.
        github.StableRelease(8, "v2", datetime(2025, 1, 1, tzinfo=timezone.utc)),
    ]
    resolve = AsyncMock(return_value=TAG)
    monkeypatch.setattr(github, "release_commit", resolve)
    monkeypatch.setattr(github, "commit_is_ancestor", AsyncMock(side_effect=lambda repo, old, new: new == MOVED))

    assert await poller._nightly_pass() == [1001, 1002]
    assert queued == [(1, TAG), (2, TAG)]
    resolve.assert_awaited_once_with("acme/shared", "v2")


async def test_known_older_out_of_branch_unknown_or_already_scanned_tag_is_not_queued(monkeypatch):
    first = datetime.now(timezone.utc)
    links = [_link(i, HEAD) for i in range(1, 6)]
    for link in links:
        link["last_success_at"] = first
    links[3]["successful_commits"] = [TAG]
    links[4]["attempted_targets"] = [{"sha": TAG, "scope": link_scope(links[4])}] * 3
    poller, queued = _poller(monkeypatch, links, {item["repo_slug"]: MOVED for item in links})
    github.stable_releases.return_value = [
        github.StableRelease(1, "v1", first),
        github.StableRelease(2, "v1-mirror", first),
    ]
    monkeypatch.setattr(github, "release_commit", AsyncMock(return_value=TAG))

    async def compare(repo, old, new):
        if repo == "acme/r1":
            return True  # already superseded by last scan
        if repo == "acme/r2":
            return False  # unrelated linked branch
        return None  # timeouts cannot establish either relation

    monkeypatch.setattr(github, "commit_is_ancestor", compare)
    assert await poller._nightly_pass() == []
    assert not queued


async def test_retry_uses_first_pinned_sha_even_if_release_tag_moves(monkeypatch):
    link = _link(1, HEAD)
    link["last_success_at"] = datetime.now(timezone.utc)
    link["workflow_release_baseline"]["observed"] = {"21": TAG}
    poller, queued = _poller(monkeypatch, [link], {link["repo_slug"]: MOVED})
    github.stable_releases.return_value = [github.StableRelease(21, "v2", datetime.now(timezone.utc))]
    lookup = AsyncMock(return_value=MOVED)
    monkeypatch.setattr(github, "release_commit", lookup)
    monkeypatch.setattr(github, "commit_is_ancestor", AsyncMock(side_effect=lambda repo, old, new: new == MOVED))

    assert await poller._nightly_pass() == [1001]
    assert queued == [(1, TAG)]
    lookup.assert_not_awaited()


async def test_configured_age_uses_last_success_not_newer_skip(monkeypatch):
    link = _link(1, HEAD)
    link["last_success_at"] = datetime.now(timezone.utc) - timedelta(days=6)
    link["last_skip_at"] = datetime.now(timezone.utc)
    poller, queued = _poller(monkeypatch, [link], {link["repo_slug"]: MOVED})
    assert await poller._nightly_pass() == []
    monkeypatch.setattr(analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_refresh_days=5))
    github.stable_releases.return_value = None  # branch proof still allows the fallback
    assert await poller._nightly_pass() == [1001]
    assert queued == [(1, MOVED)]


def test_refresh_age_defaults_on_invalid_environment_and_accepts_override(monkeypatch):
    for value in ("", "banana", "0", "-20", "999999999"):
        monkeypatch.setenv("LOGOS_AGENT_ANALYSIS_REFRESH_DAYS", value)
        assert Settings().analysis_refresh_days == 30
    monkeypatch.setenv("LOGOS_AGENT_ANALYSIS_REFRESH_DAYS", "47")
    assert Settings().analysis_refresh_days == 47


async def test_stable_discovery_filters_drafts_and_prereleases_and_orders_publication(monkeypatch):
    def release(ident, at, **flags):
        return {"id": ident, "tag_name": f"v{ident}", "published_at": at, "draft": False, "prerelease": False} | flags

    mocked = AsyncMock(
        return_value=(
            [
                release(1, "2025-03-01T00:00:00Z"),
                release(2, "2025-02-01T00:00:00Z"),
                release(3, "2025-04-01T00:00:00Z", prerelease=True),
                release(4, "2025-05-01T00:00:00Z", draft=True),
                release(5, None),
            ],
            False,
        )
    )
    monkeypatch.setattr(github, "_get_all_bounded", mocked)
    assert [item.id for item in await github.stable_releases("acme/r")] == [1, 2]
    mocked.return_value = (mocked.return_value[0], True)
    assert await github.stable_releases("acme/r") is None


async def test_annotated_tags_resolve_to_the_commit(monkeypatch):
    get = AsyncMock(side_effect=[{"object": {"type": "tag", "sha": HEAD}}, {"object": {"type": "commit", "sha": TAG}}])
    monkeypatch.setattr(github, "_get", get)
    assert await github.release_commit("acme/r", "release/v2") == TAG
    assert [call.args[0] for call in get.await_args_list] == [
        "/repos/acme/r/git/ref/tags/release%2Fv2",
        f"/repos/acme/r/git/tags/{HEAD}",
    ]


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"ref": "refs/heads/feature/x", "object": {"type": "commit", "sha": self.text.strip()}}


def _fake_client(response: _FakeResponse, calls: list):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, headers=None):
            calls.append((url, headers))
            return response

    return Client


async def test_branch_head_reads_the_sha_and_says_none_when_github_will_not(monkeypatch):
    calls: list = []
    monkeypatch.setattr(github.httpx, "AsyncClient", _fake_client(_FakeResponse(200, HEAD + "\n"), calls))
    assert await github.branch_head("acme/r", "feature/x") == HEAD
    assert calls[0][0].endswith("/repos/acme/r/git/ref/heads/feature%2Fx")
    assert calls[0][1]["Accept"] == "application/vnd.github+json"

    monkeypatch.setattr(github.httpx, "AsyncClient", _fake_client(_FakeResponse(404, "Not Found"), []))
    assert await github.branch_head("acme/private", "main") is None


async def test_a_failed_nightly_pass_is_retried_within_the_hour(monkeypatch):
    monkeypatch.setattr(analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=1))
    poller = AnalysisPoller()

    async def nothing(*args, **kwargs):
        return []

    async def boom():
        raise RuntimeError("db down")

    class _Control:
        def admission_block(self):
            return ""

    async def current():
        return _Control()

    monkeypatch.setattr(poller, "_reconcile_stale_analyses", nothing)
    monkeypatch.setattr(poller, "_links_needing_analysis", nothing)
    monkeypatch.setattr(poller, "_nightly_pass", boom)
    monkeypatch.setattr(analysis_triggers.controls, "current", current)
    monkeypatch.setattr(analysis_triggers.model_policy, "current", lambda: type("P", (), {"ok": True, "detail": ""})())

    night = datetime(2026, 10, 3, 1, 5, tzinfo=timezone.utc)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return night

    monkeypatch.setattr(analysis_triggers, "datetime", _Clock)
    try:
        await poller.poll_once()
    except RuntimeError:
        pass
    assert poller._nightly_due(night.replace(minute=10))


async def test_a_stopped_runner_still_queues_analysis(monkeypatch):
    # Pause stops starting work, not writing it down: a link that needs
    # analysis while the runner is stopped must still become a queued
    # session so it is waiting when the runner comes back.
    monkeypatch.setattr(
        analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=-1)
    )
    poller = AnalysisPoller()
    link = _link(1, None)
    queued_links: list[dict] = []

    async def nothing(*args, **kwargs):
        return None

    async def needing():
        return [link]

    async def queue(item, *, target=None):
        queued_links.append(item)
        return 42

    class _Control:
        def admission_block(self):
            return "the runner is paused"

    async def current():
        return _Control()

    monkeypatch.setattr(poller, "_reconcile_stale_analyses", nothing)
    monkeypatch.setattr(poller, "_links_needing_analysis", needing)
    monkeypatch.setattr(poller, "_queue", queue)
    monkeypatch.setattr(analysis_triggers.controls, "current", current)
    monkeypatch.setattr(analysis_triggers.model_policy, "current", lambda: type("P", (), {"ok": True, "detail": ""})())

    sessions = await poller.poll_once()

    assert sessions == [42]
    assert queued_links == [link]


async def test_a_stopped_runner_still_queues_nightly_analysis(monkeypatch):
    monkeypatch.setattr(analysis_triggers, "settings", replace(analysis_triggers.settings, analysis_nightly_hour_utc=1))
    poller = AnalysisPoller()
    link = _link(2, HEAD)
    queued: list[tuple[dict, str | None]] = []

    async def nothing(*args, **kwargs):
        return []

    async def for_nightly():
        return [link]

    async def queue(item, *, target=None):
        queued.append((item, target))
        return 99

    async def head(slug, branch):
        return MOVED

    class _Control:
        def admission_block(self):
            return "no new sessions while draining"

    async def current():
        return _Control()

    night = datetime(2026, 10, 3, 1, 5, tzinfo=timezone.utc)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return night

    monkeypatch.setattr(poller, "_reconcile_stale_analyses", nothing)
    monkeypatch.setattr(poller, "_links_needing_analysis", nothing)
    monkeypatch.setattr(poller, "_links_for_nightly", for_nightly)
    monkeypatch.setattr(poller, "_queue", queue)
    monkeypatch.setattr(github, "branch_head", head)
    monkeypatch.setattr(github, "stable_releases", AsyncMock(return_value=[]))
    monkeypatch.setattr(analysis_triggers.controls, "current", current)
    monkeypatch.setattr(analysis_triggers.model_policy, "current", lambda: type("P", (), {"ok": True, "detail": ""})())
    monkeypatch.setattr(analysis_triggers, "datetime", _Clock)

    link["last_success_at"] = night - timedelta(days=31)
    sessions = await poller.poll_once()

    assert sessions == [99]
    assert queued == [(link, {"sha": MOVED, "scope": link_scope(link), "kind": "branch"})]
    assert poller._nightly_done_for == night.date()


async def test_due_fallback_refreshes_a_rolled_back_branch_but_release_does_not_replay_it(monkeypatch):
    link = _link(1, MOVED)
    link["successful_commits"] = [HEAD, MOVED]
    link["last_success_at"] = datetime.now(timezone.utc) - timedelta(days=29)
    poller, queued = _poller(monkeypatch, [link], {link["repo_slug"]: HEAD})
    github.stable_releases.return_value = [github.StableRelease(3, "v3", datetime.now(timezone.utc))]
    monkeypatch.setattr(github, "release_commit", AsyncMock(return_value=HEAD))

    # A newly published tag must not replay an already successful release target.
    assert await poller._nightly_pass() == []

    # After the interval it is a changed branch, relative to the latest successful B.
    link["last_success_at"] -= timedelta(days=2)
    assert await poller._nightly_pass() == [1001]
    assert queued == [(1, HEAD)]
