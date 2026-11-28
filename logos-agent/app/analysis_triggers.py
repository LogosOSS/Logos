"""Queue read-only agent sessions that analyse linked team repositories.

Linked application repos (``team_repositories``) need AI-workflow diagrams and
SLO recommendations. Owners can trigger that from the UI; this poller also
queues spare-capacity analysis when a link has no succeeded analysis yet, and
once a night for newly published stable releases or sufficiently old analyses
whose linked branch has moved.

Sessions are ``no_push``, ``trigger_kind=analysis``, low priority, and write
``/artifacts/analysis.json`` for :mod:`analysis_ingest`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import text

from . import controls, db, github, model_policy
from .analysis_cadence import MAX_TARGET_ATTEMPTS, commit_sha, link_scope
from .asyncio_tasks import cancel_and_wait
from .config import settings
from .schemas import ACTIVE_STATUSES, SessionStatus

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 300.0
# A queued analysis row is attached to its session within one queue call.
UNATTACHED_SLOT_TTL = timedelta(minutes=10)
CREATED_BY = "logos-agent (analysis)"
# Below issue (50) / comment (70) so application analysis yields to Logos work.
ANALYSIS_PRIORITY = 10
ANALYSIS_PRIORITY_REASON = "workflow analysis — spare capacity"


ANALYSIS_TASK = """\
Analyse this linked application repository for AI / LLM call sites and produce
workflow diagrams plus SLO recommendations.

Write structured results ONLY to `/artifacts/analysis.json` (UTF-8 JSON).
Do not push commits, open pull requests, or modify the remote.

Schema for `/artifacts/analysis.json`:
{{
  "commit_sha": "<git HEAD sha of the checkout you analysed>",
  "workflows": [
    {{
      "name": "<short workflow group name>",
      "trigger_summary": "<what starts this flow>",
      "diagram_mermaid": "flowchart TD\\n  A-->B",
      "sort_order": 0
    }}
  ],
  "recommendations": [
    {{
      "workflow": "<matching workflows[].name>",
      "file_path": "path/from/repo/root.py",
      "start_line": 1,
      "end_line": 20,
      "code_url": "optional permalink to the call site, or null",
      "detected_model": "optional model name or null",
      "recommended_slo": "ux-critical" | "ux-high-prio" | "ux-background",
      "objective_priority": ["latency" | "quality" | "price", "..."],
      "confidence": 0.0,
      "justification": "why this SLO and objective order",
      "traffic_flags": {{"night_heavy": false}}
    }}
  ]
}}

`diagram_mermaid` must parse with Mermaid 11: wrap every node and edge label
in double quotes (`A["Session title LLM (deferred)"]`, `B{{"EXERCISE mode?"}}`,
`A -->|"yes"| B`). Parentheses, brackets, braces, pipes or slashes inside an
unquoted label are syntax errors and the diagram will not render.

`objective_priority` is a full ranking of latency, quality, and price (most
important first). It complements SLO: SLO is urgency/interactivity; the ranking
says what to optimize for when choosing a model. If omitted, defaults are:
ux-critical → [latency, quality, price]; ux-high-prio → [quality, latency, price];
ux-background → [price, quality, latency].

Repository: {repo_slug}
Clone URL: {repo_url}
Focus paths (empty means whole tree): {paths}
"""


class AnalysisPoller:
    """Periodically queues analysis sessions for team repository links."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._last_error: str = ""
        self._last_pass: datetime | None = None
        self._queued_total = 0
        # The schedule is in memory; the release baseline and attempt
        # history in PostgreSQL also deduplicate a pass after restart.
        self._nightly_done_for: date | None = None
        self._skipped_unchanged_total = 0
        self.on_queued: Callable[[], Awaitable[None]] | None = None

    async def start(self) -> None:
        if not settings.triggers_enabled:
            logger.info("analysis poller off (LOGOS_AGENT_TRIGGERS_ENABLED=false)")
            return
        self._task = asyncio.create_task(self._loop(), name="agent-analysis-triggers")
        logger.info("watching team repositories for workflow analysis every %.0fs", POLL_INTERVAL_S)

    async def stop(self) -> None:
        self._stopping.set()
        await cancel_and_wait(self._task)
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning("analysis poll failed: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> list[int]:
        """Queue analysis for links that still need it. Returns session ids."""
        now = datetime.now(timezone.utc)
        await self._reconcile_stale_analyses()
        control = await controls.current()
        # Pause and drain stop *starting* work, not writing it down. A link
        # that needs analysis while the runner is paused still becomes a
        # queued session so it is waiting when the runner comes back.
        blocked = control.admission_block()
        if blocked:
            logger.debug("analysis poll queueing while stopped: %s", blocked)
        policy = model_policy.current()
        if not policy.ok:
            logger.debug("analysis poll skipped: %s", policy.detail)
            self._last_pass = now
            return []

        links = await self._links_needing_analysis()
        self._last_pass = now
        self._last_error = ""

        queued: list[int] = []
        for link in links:
            session_id = await self._queue(link)
            if session_id is not None:
                queued.append(session_id)
        if self._nightly_due(now):
            queued.extend(await self._nightly_pass())
            # Only a pass that got through counts: one that raised is retried
            # by the next poll within the same hour.
            self._nightly_done_for = now.date()
        if not queued:
            return []
        if queued and self.on_queued is not None:
            try:
                await self.on_queued()
            except Exception as exc:
                logger.info("scheduler nudge after analysis queue failed: %s", exc)
        return queued

    def _nightly_due(self, now: datetime) -> bool:
        hour = settings.analysis_nightly_hour_utc
        return 0 <= hour <= 23 and now.hour == hour and self._nightly_done_for != now.date()

    async def _nightly_pass(self) -> list[int]:
        """Refresh proven targets without treating an unreadable branch as changed."""
        now = datetime.now(timezone.utc)
        queued: list[int] = []
        skipped = 0
        release_lists: dict[str, list[github.StableRelease] | None] = {}
        branch_heads: dict[tuple[str, str], str | None] = {}
        tag_commits: dict[tuple[str, str], str | None] = {}
        comparisons: dict[tuple[str, str, str], bool | None] = {}

        async def ancestor(slug: str, older: str, newer: str) -> bool | None:
            key = (slug, older, newer)
            if key not in comparisons:
                comparisons[key] = await github.commit_is_ancestor(*key)
            return comparisons[key]

        for link in await self._links_for_nightly():
            slug = str(link["repo_slug"])
            scope = link_scope(link)
            key = (slug, str(link.get("branch") or "main"))
            if key not in branch_heads:
                branch_heads[key] = await github.branch_head(*key)
            head = branch_heads[key]
            previous = commit_sha(link.get("last_commit"))
            covered = set(link.get("successful_commits") or [])
            attempted = Counter(
                item["sha"] for item in (link.get("attempted_targets") or []) if item.get("scope") == scope
            )
            blocked = {sha for sha, count in attempted.items() if count >= MAX_TARGET_ATTEMPTS}

            if slug not in release_lists:
                release_lists[slug] = await github.stable_releases(slug)
            releases = release_lists[slug]
            baseline = link.get("workflow_release_baseline") or {}
            target = None
            if releases is not None:
                if baseline.get("scope") != scope:
                    # The first complete observation is a rollout baseline,
                    # not new work. Keep stable identities rather than a date
                    # so an older prerelease promoted later is still visible.
                    await self._seed_release_baseline(link, scope, [release.id for release in releases])
                elif head:
                    known = set(baseline["release_ids"])
                    observed = baseline.get("observed") or {}
                    for release in releases:
                        if release.id in known:
                            continue
                        sha = commit_sha(observed.get(str(release.id)))
                        if not sha:
                            tag = (slug, release.tag)
                            if tag not in tag_commits:
                                tag_commits[tag] = await github.release_commit(*tag)
                            if tag_sha := tag_commits[tag]:
                                sha = await self._record_release_target(link, scope, release.id, tag_sha)
                        if not sha or sha in covered or sha in blocked or sha == previous:
                            continue
                        if await ancestor(slug, sha, head) is not True:
                            continue
                        # Don't replace newer results with a late-published
                        # older release. Unknown ancestry is not permission.
                        if previous and await ancestor(slug, sha, previous) is not False:
                            continue
                        target = sha
                        break

            last_success_at = link.get("last_success_at")
            due = last_success_at is not None and now - last_success_at >= timedelta(
                days=settings.analysis_refresh_days
            )
            if target is None and due and head:
                if head == previous or head in covered:
                    skipped += 1
                elif previous and head not in blocked:
                    target = head
            if target is not None:
                session_id = await self._queue(link, target={"sha": target, "scope": scope})
                if session_id is not None:
                    queued.append(session_id)
        self._skipped_unchanged_total += skipped
        logger.info("nightly analysis pass: %s queued, %s unchanged", len(queued), skipped)
        return queued

    async def _seed_release_baseline(self, link: dict[str, Any], scope: str, release_ids: list[int]) -> None:
        async with db.sessionmaker()() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT repo_slug, branch, paths, automatic_workflow_analysis FROM team_repositories "
                            "WHERE id = :id FOR UPDATE"
                        ),
                        {"id": link["id"]},
                    )
                )
                .mappings()
                .first()
            )
            if row is None or not row["automatic_workflow_analysis"] or link_scope(row) != scope:
                return
            await conn.execute(
                text("""
                    UPDATE team_repositories SET workflow_release_baseline =
                        jsonb_build_object('scope', CAST(:scope AS text), 'release_ids', CAST(:ids AS bigint[]))
                     WHERE id = :id AND workflow_release_baseline->>'scope' IS DISTINCT FROM :scope
                    """),
                {"id": link["id"], "scope": scope, "ids": release_ids},
            )
            await conn.commit()

    async def _record_release_target(self, link: dict[str, Any], scope: str, release_id: int, sha: str) -> str | None:
        """Freeze the first resolution of a release identity independently of scan success."""
        async with db.sessionmaker()() as conn:
            stored = await conn.scalar(
                text("""
                    UPDATE team_repositories
                       SET workflow_release_baseline = jsonb_set(
                           workflow_release_baseline, '{observed}',
                           COALESCE(workflow_release_baseline->'observed', '{}'::jsonb) ||
                           jsonb_build_object(CAST(:release AS text), COALESCE(
                               workflow_release_baseline->'observed'->>CAST(:release AS text), CAST(:sha AS text))))
                     WHERE id = :id AND automatic_workflow_analysis
                       AND workflow_release_baseline->>'scope' = :scope
                 RETURNING workflow_release_baseline->'observed'->>CAST(:release AS text)
                    """),
                {"id": link["id"], "scope": scope, "release": str(release_id), "sha": sha},
            )
            await conn.commit()
        return commit_sha(stored)

    async def _links_for_nightly(self) -> list[dict[str, Any]]:
        """Repositories with a succeeded analysis and nothing in flight, with that analysis' commit."""
        async with db.sessionmaker()() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                        SELECT tr.id, tr.team_id, tr.repo_url, tr.repo_slug, tr.branch, tr.paths,
                               tr.workflow_release_baseline, latest.commit_sha AS last_commit,
                               latest.finished_at AS last_success_at,
                               ARRAY(SELECT a.commit_sha FROM ai_workflow_analyses a
                                      WHERE a.team_repository_id = tr.id AND a.status = 'succeeded'
                                        AND a.commit_sha IS NOT NULL) AS successful_commits,
                               ARRAY(SELECT s.workflow_analysis_target
                                      FROM agent_sessions s JOIN ai_workflow_analyses a ON a.agent_session_id = s.id
                                     WHERE a.team_repository_id = tr.id AND a.automatically_queued
                                       AND s.workflow_analysis_target IS NOT NULL) AS attempted_targets
                          FROM team_repositories tr
                          JOIN LATERAL (
                              SELECT a.commit_sha, a.finished_at
                                FROM ai_workflow_analyses a
                               WHERE a.team_repository_id = tr.id AND a.status = 'succeeded'
                               ORDER BY a.finished_at DESC NULLS LAST, a.id DESC LIMIT 1
                          ) latest ON true
                         WHERE tr.automatic_workflow_analysis
                           AND NOT EXISTS (
                                 SELECT 1 FROM ai_workflow_analyses a
                                  WHERE a.team_repository_id = tr.id
                                    AND a.status IN ('queued', 'running')
                               )
                           AND NOT EXISTS (
                                 SELECT 1 FROM agent_sessions s
                                  WHERE s.team_repository_id = tr.id
                                    AND s.trigger_kind = 'analysis'
                                    AND s.status = ANY(:active)
                               )
                         ORDER BY tr.id
                        """),
                        {"active": [s.value for s in ACTIVE_STATUSES]},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def _reconcile_stale_analyses(self) -> None:
        """Mark queued/running analyses failed when their session is terminal."""
        terminal = [
            SessionStatus.FAILED.value,
            SessionStatus.CANCELLED.value,
            SessionStatus.SUCCEEDED.value,
        ]
        async with db.sessionmaker()() as conn:
            await conn.execute(
                text("""
                    UPDATE ai_workflow_analyses a
                       SET status = 'failed',
                           error = COALESCE(a.error, 'agent session ended without ingest'),
                           finished_at = COALESCE(a.finished_at, :now)
                      FROM agent_sessions s
                     WHERE a.agent_session_id = s.id
                       AND a.status IN ('queued', 'running')
                       AND s.status = ANY(:terminal)
                       AND NOT EXISTS (
                             SELECT 1 FROM ai_workflow_analyses ok
                              WHERE ok.agent_session_id = s.id
                                AND ok.status = 'succeeded'
                           )
                    """),
                {"now": datetime.now(timezone.utc), "terminal": terminal},
            )
            # Succeeded sessions that never produced a succeeded analysis row
            # (ingest skipped / failed) are covered above. Also clear orphaned
            # queued rows whose session row is gone.
            await conn.execute(
                text("""
                    UPDATE ai_workflow_analyses a
                       SET status = 'failed',
                           error = COALESCE(a.error, 'agent session missing'),
                           finished_at = COALESCE(a.finished_at, :now)
                     WHERE a.status IN ('queued', 'running')
                       AND a.agent_session_id IS NOT NULL
                       AND NOT EXISTS (
                             SELECT 1 FROM agent_sessions s WHERE s.id = a.agent_session_id
                           )
                    """),
                {"now": datetime.now(timezone.utc)},
            )
            # A claimed slot whose session never got attached (the runner died
            # in between) would block the repository for good.
            await conn.execute(
                text("""
                    UPDATE ai_workflow_analyses
                       SET status = 'failed',
                           error = COALESCE(error, 'queued without a session'),
                           finished_at = COALESCE(finished_at, :now)
                     WHERE status = 'queued'
                       AND agent_session_id IS NULL
                       AND started_at < :stale_before
                    """),
                {"now": datetime.now(timezone.utc), "stale_before": datetime.now(timezone.utc) - UNATTACHED_SLOT_TTL},
            )
            await conn.commit()

    async def _links_needing_analysis(self) -> list[dict[str, Any]]:
        """Team repos with no succeeded analysis, and nothing already in flight.

        Any succeeded row counts as covered here; analysed repositories are
        revisited by the nightly pass, which compares the branch head with the
        analysed commit. A failed-only history is re-queued once nothing is
        queued/running.
        """
        async with db.sessionmaker()() as conn:
            rows = (
                (
                    await conn.execute(
                        text("""
                        SELECT tr.id, tr.team_id, tr.repo_url, tr.repo_slug,
                               tr.branch, tr.paths
                          FROM team_repositories tr
                         WHERE tr.automatic_workflow_analysis
                           AND NOT EXISTS (
                                 SELECT 1 FROM ai_workflow_analyses a
                                  WHERE a.team_repository_id = tr.id
                                    AND a.status = 'succeeded'
                               )
                           AND NOT EXISTS (
                                 SELECT 1 FROM ai_workflow_analyses a
                                  WHERE a.team_repository_id = tr.id
                                    AND a.status IN ('queued', 'running')
                               )
                           AND NOT EXISTS (
                                 SELECT 1 FROM agent_sessions s
                                  WHERE s.team_repository_id = tr.id
                                    AND s.trigger_kind = 'analysis'
                                    AND s.status = ANY(:active)
                               )
                         ORDER BY tr.id
                         LIMIT 20
                        """),
                        {"active": [s.value for s in ACTIVE_STATUSES]},
                    )
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    async def _queue(self, link: dict[str, Any], *, target: dict[str, str] | None = None) -> int | None:
        link_id = int(link["id"])
        team_id = int(link["team_id"])
        repo_url = str(link["repo_url"])
        repo_slug = str(link["repo_slug"])
        branch = str(link.get("branch") or "main")
        paths = link.get("paths")
        paths_label = ", ".join(paths) if isinstance(paths, list) and paths else "(entire repository)"
        trigger_ref = f"team-repository:{link_id}"
        if target:
            trigger_ref += f":{target['scope']}:{target['sha']}"

        workspace_id = await self._ensure_workspace(team_id, link_id, branch)
        if workspace_id is None:
            return None

        task = ANALYSIS_TASK.format(
            repo_slug=repo_slug,
            repo_url=repo_url,
            paths=paths_label,
        )
        if target:
            task += f"\nAnalyse the prepared checkout at commit {target['sha']}. Do not switch revisions.\n"
        try:
            session_id = await db.create_session(
                workspace_id=workspace_id,
                task=task,
                model=None,
                created_by=CREATED_BY,
                open_pull_request=False,
                deploy_to_dev=False,
                screenshot_paths=[],
                no_push=True,
                trigger_kind="analysis",
                trigger_ref=trigger_ref,
                priority=ANALYSIS_PRIORITY,
                priority_reason=ANALYSIS_PRIORITY_REASON,
                repo_url=repo_url,
                repo_slug=repo_slug,
                team_repository_id=link_id,
                repo_base_branch=branch if target else None,
                workflow_analysis_target=target,
                automatic_analysis=True,
            )
        except ValueError as exc:
            logger.warning("could not queue analysis for team_repository %s: %s", link_id, exc)
            return None

        self._queued_total += 1
        logger.info("queued analysis session %s for %s", session_id, repo_slug)
        return session_id

    async def _ensure_workspace(self, team_id: int, link_id: int, branch: str) -> int | None:
        name = f"analysis-team-{team_id}-repo-{link_id}"
        try:
            # Upsert always so a reused / revived workspace picks up branch edits.
            created = await db.create_workspace(
                name=name,
                base_branch=branch,
                created_by=CREATED_BY,
                ephemeral=True,
            )
            return int(created["id"])
        except ValueError:
            # Live workspace of the same name: update base_branch in place.
            async with db.sessionmaker()() as conn:
                row = (
                    (
                        await conn.execute(
                            text("""
                            UPDATE agent_workspaces
                               SET base_branch = :branch
                             WHERE name = :name AND archived_at IS NULL
                         RETURNING id
                            """),
                            {"name": name, "branch": branch},
                        )
                    )
                    .mappings()
                    .first()
                )
                await conn.commit()
            if row is None:
                logger.info("analysis workspace %s unavailable", name)
                return None
            return int(row["id"])

    def status(self) -> dict[str, Any]:
        return {
            "enabled": settings.triggers_enabled,
            "polling": self._task is not None and not self._task.done(),
            "poll_interval_s": POLL_INTERVAL_S,
            "last_pass": self._last_pass.isoformat() if self._last_pass else None,
            "queued_total": self._queued_total,
            "nightly_hour_utc": settings.analysis_nightly_hour_utc,
            "nightly_done_for": self._nightly_done_for.isoformat() if self._nightly_done_for else None,
            "skipped_unchanged_total": self._skipped_unchanged_total,
            "last_error": self._last_error,
        }


poller = AnalysisPoller()
