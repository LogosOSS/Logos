"""The agent runner's HTTP surface.

Reached from the UI through Traefik at /api/agent/*. Every route except the
health check requires a Keycloak token carrying the operator role.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from . import (
    analysis_triggers,
    capacity,
    controls,
    conventions,
    db,
    docker_engine,
    github,
    memory_budget,
    memory_usage,
    model_policy,
    pulse,
    repository_context,
    security_incidents,
    security_scans,
    security_store,
    triggers,
    workspace_storage,
)
from .artifacts import read_artifact
from .auth import Principal, require_agent_operator
from .config import settings
from .schemas import (
    TERMINAL_STATUSES,
    CapacityState,
    CodingRepository,
    ControlState,
    ControlUpdate,
    EventKind,
    InstructionState,
    InstructionUpdate,
    QueueMove,
    SessionCreate,
    SessionEvent,
    SessionMessages,
    SessionStatus,
    SessionSummary,
    Workspace,
    WorkspaceCreate,
    WorkspaceStorage,
)
from .security_models import AssessmentAction, IncidentInput, RepositoryInput
from .sessions import artifact_dir, cache_volume_for, manager, memory_admission_block
from .sessions import reserved_memory_mb as sessions_reserved_memory_mb
from .sessions import resumption_base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("logos.agent")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.auth_disabled and not settings.dev_mode:
        raise RuntimeError(
            "LOGOS_AGENT_AUTH_DISABLED is set without LOGOS_AGENT_DEV_MODE. "
            "Refusing to start an unauthenticated agent runner."
        )
    if settings.pause_above_load <= settings.start_below_load:
        raise RuntimeError(
            "LOGOS_AGENT_PAUSE_ABOVE_LOAD must exceed LOGOS_AGENT_START_BELOW_LOAD, "
            "otherwise sessions start and pause in a loop."
        )
    # A token that belongs to somebody else would put agent commits and pull
    # requests under their name, which no later check can undo — so it stops
    # the service here.
    for note in await github.verify_identities():
        logger.info("github identity: %s", note)
    # The local-only model policy does not stop startup: the UI has to come
    # up to *show* the reason, and admission is gated on it anyway.
    policy = await model_policy.refresh()
    if not policy.ok:
        logger.error("no session will start until the model policy is satisfied: %s", policy.detail)
    if settings.session_github_token and settings.session_github_token == settings.github_token:
        logger.warning(
            "session containers get the runner's own GitHub token: it can dispatch "
            "workflows and edit workflow files. Issue a second token of the same "
            "account without 'workflow' scope as LOGOS_AGENT_SESSION_GITHUB_TOKEN to "
            "keep that out of the agent's reach."
        )
    await memory_budget.establish()
    await manager.start()
    triggers.poller.on_queued = manager.scheduler_pass
    await triggers.poller.start()
    analysis_triggers.poller.on_queued = manager.scheduler_pass
    await analysis_triggers.poller.start()
    await security_incidents.poller.start()
    logger.info(
        "agent runner ready: max %s parallel sessions, start below %.0f%% load, " "pause above %.0f%%",
        settings.max_parallel_sessions,
        settings.start_below_load * 100,
        settings.pause_above_load * 100,
    )
    try:
        yield
    finally:
        await security_incidents.poller.stop()
        await analysis_triggers.poller.stop()
        await triggers.poller.stop()
        await manager.stop()


app = FastAPI(
    title="Logos Agent Runner",
    description=(
        "Runs coding agents in isolated containers on capacity Logos is not "
        "otherwise using. Sessions act on the dev environment only."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


# --- health ---------------------------------------------------------------


@app.get("/security", tags=["security"])
async def get_security(_: Principal = Depends(require_agent_operator)) -> dict:
    return {
        **await security_store.overview(),
        "enabled": settings.security_enabled,
        "feeds_enabled": settings.security_feeds_enabled,
        "poll_error": security_incidents.poller.last_error,
    }


@app.post("/security/repositories", status_code=201, tags=["security"])
async def add_security_repository(
    body: RepositoryInput, principal: Principal = Depends(require_agent_operator)
) -> dict:
    # Explicit admin enrollment is the repository authorization boundary.
    # Team-controlled clone URLs never enter credentialed security sessions.
    try:
        return await security_store.enroll(body, principal.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/security/incidents", status_code=201, tags=["security"])
async def add_security_incident(body: IncidentInput, principal: Principal = Depends(require_agent_operator)) -> dict:
    return await security_store.ingest(body, principal.username, manual=True)


@app.get("/security/assessments/{assessment_id}/events", tags=["security"])
async def security_history(assessment_id: int, _: Principal = Depends(require_agent_operator)) -> list[dict]:
    return await security_store.history(assessment_id)


@app.post("/security/assessments/{assessment_id}/actions", tags=["security"])
async def security_action(
    assessment_id: int, body: AssessmentAction, principal: Principal = Depends(require_agent_operator)
) -> dict:
    try:
        found = await security_store.act(assessment_id, body.action, body.reason, principal.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not found:
        raise HTTPException(404, "Assessment not found")
    return {"ok": True}


@app.get("/health", tags=["monitoring"])
async def health() -> dict[str, object]:
    database_ok = await db.ping()
    docker_ok = await docker_engine.ping()
    healthy = database_ok and docker_ok
    return {
        "status": "ok" if healthy else "degraded",
        "database": database_ok,
        "docker": docker_ok,
    }


# --- what every session is told -------------------------------------------


@app.get("/instructions", response_model=InstructionState, tags=["capacity"])
async def get_instructions(_: Principal = Depends(require_agent_operator)) -> InstructionState:
    """The standing text appended to every task, as it stands now."""
    return InstructionState(**vars(await conventions.current()))


@app.put("/instructions", response_model=InstructionState, tags=["capacity"])
async def put_instructions(
    body: InstructionUpdate,
    principal: Principal = Depends(require_agent_operator),
) -> InstructionState:
    """Change what every session is told, without a deployment.

    These are prompts: the part of an unattended agent most worth adjusting
    after watching it work, and the part least worth waiting for a release
    to adjust. A half that is reset goes back to what the code ships with,
    which is not the same as an empty one — empty is somebody deciding that
    nothing should be said there.

    A half nobody mentioned stays as it is stored. Resetting the house
    rules is a statement about the house rules, and it must not carry an
    untouched draft of the environment notes into the database with it.
    """
    sent = body.model_fields_set

    def half(name: str, value: str | None, reset: bool) -> str | None | db.Unchanged:
        if reset:
            return None
        return value if name in sent else db.UNCHANGED

    state = await conventions.set_instructions(
        house_rules=half("house_rules", body.house_rules, body.reset_house_rules),
        environment_notes=half("environment_notes", body.environment_notes, body.reset_environment_notes),
        by=principal.username or "an operator",
    )
    logger.info("agent instructions changed by %s", principal.username)
    return InstructionState(**vars(state))


# --- capacity -------------------------------------------------------------


@app.get("/capacity", response_model=CapacityState, tags=["capacity"])
async def get_capacity(_: Principal = Depends(require_agent_operator)) -> CapacityState:
    # Admission uses the measured figure (who filled the lane does not
    # matter). Own-slot colouring on the page still needs the per-key split,
    # so the key is passed with discount_own=False.
    own_key = await db.agent_key_id()
    reading = await capacity.read_load(
        lane=model_policy.current().lane(),
        own_api_key_id=own_key,
        discount_own=False,
    )
    counts = await db.count_sessions_by_status()
    running = counts.get(SessionStatus.RUNNING.value, 0)
    paused = counts.get(SessionStatus.PAUSED.value, 0)
    control = await controls.current()
    next_session = await db.next_queued_session() if counts.get(SessionStatus.QUEUED.value, 0) else None
    memory_override = (next_session or {}).get("memory_override_mb")
    max_parallel = control.parallel_for(memory_override)
    may_start, reason = capacity.start_decision(reading, running=running, paused=paused, max_parallel=max_parallel)
    blocked = control.admission_block(memory_override)
    if blocked:
        may_start, reason = False, blocked
    elif may_start:
        memory_block = await memory_admission_block(control, override_mb=memory_override)
        if memory_block:
            may_start, reason = False, memory_block
    # The local-only model policy gates admission as hard as load does, so
    # the page that explains why nothing starts has to show it too — an
    # operator staring at an idle platform should not have to read the logs
    # to learn that the agent key was granted a cloud provider.
    policy = await model_policy.refresh()
    if not policy.ok:
        may_start, reason = False, policy.detail
    # own_slots and busy_slots share one population (see capacity.parse):
    # partition against busy so agent + other + free always add up.
    own_slots = max(0, min(reading.own_slots, reading.busy_slots))
    other_slots = max(0, reading.busy_slots - own_slots)
    return CapacityState(
        models_local_only=policy.ok,
        models_detail=policy.detail,
        load=round(reading.load, 4),
        total_slots=reading.total_slots,
        busy_slots=reading.busy_slots,
        own_slots=own_slots,
        other_slots=other_slots,
        sessions_running=running,
        sessions_queued=counts.get(SessionStatus.QUEUED.value, 0),
        sessions_paused=paused,
        max_parallel=max_parallel,
        may_start=may_start,
        reason=reason,
    )


async def _control_state(state: controls.Controls) -> ControlState:
    budget = memory_budget.current()
    reserved = 0
    if budget is not None:
        try:
            reserved = await sessions_reserved_memory_mb()
        except Exception as exc:  # noqa: BLE001 - the page still shows the controls
            logger.info("could not read the reserved session memory: %s", exc)
    return ControlState(
        mode=state.mode,
        mode_reason=state.mode_reason,
        paused=state.paused,
        admits_new_sessions=not state.admission_block(),
        max_parallel=state.max_parallel,
        max_parallel_override=state.max_parallel_override,
        max_parallel_configured=settings.max_parallel_sessions,
        max_parallel_available=state.max_parallel_available,
        session_memory_mb=state.session_memory_mb,
        session_memory_override=state.session_memory_override,
        session_memory_configured=settings.session_memory_mb,
        memory_budget_mb=budget.budget_mb if budget is not None else None,
        host_memory_mb=budget.host_mb if budget is not None else None,
        memory_reserved_mb=reserved,
        updated_by=state.updated_by,
    )


@app.get("/controls", response_model=ControlState, tags=["capacity"])
async def get_controls(_: Principal = Depends(require_agent_operator)) -> ControlState:
    """The kill switch and the ceiling, as they stand."""
    return await _control_state(await controls.current())


@app.post("/controls", response_model=ControlState, tags=["capacity"])
async def update_controls(body: ControlUpdate, principal: Principal = Depends(require_agent_operator)) -> ControlState:
    """Stop the runner, drain it, or change how much of the platform it uses.

    `draining` starts nothing new and lets what is running finish;
    `paused` hands everything back on the next scheduler pass. Neither
    cancels anything, so going back to `running` resumes the work mid-task.
    """
    state = await controls.current()
    if body.mode is not None:
        state = await controls.set_mode(mode=body.mode, reason=body.reason, by=principal.username)
        logger.info(
            "%s set the agent runner to %s%s",
            principal.username,
            body.mode,
            f": {body.reason}" if body.reason else "",
        )
    if body.clear_max_parallel or body.max_parallel is not None:
        limit = None if body.clear_max_parallel else body.max_parallel
        state = await controls.set_max_parallel(limit=limit, by=principal.username)
        logger.info("%s set the parallel ceiling to %s", principal.username, limit if limit is not None else "default")
    if body.clear_session_memory_mb or body.session_memory_mb is not None:
        memory = None if body.clear_session_memory_mb else body.session_memory_mb
        state = await controls.set_session_memory(memory_mb=memory, by=principal.username)
        logger.info("%s set the memory per session to %s", principal.username, f"{memory} MiB" if memory else "default")
    # A pause takes effect on the next pass anyway; running one now means the
    # operator sees it happen instead of waiting a tick for it.
    manager.request_scheduler_pass()
    return await _control_state(state)


@app.get("/models", tags=["capacity"])
async def get_models(_: Principal = Depends(require_agent_operator)) -> dict[str, object]:
    """The models a session may be driven by, and the default among them.

    Only locally served ones are ever listed: this is the same policy that
    gates admission, so what the form offers is exactly what will be
    accepted.
    """
    policy = await model_policy.refresh()
    return {
        "models": list(policy.offered),
        "default": policy.default_model,
        "local_only": policy.ok,
        "detail": policy.detail,
    }


@app.get("/triggers", tags=["capacity"])
async def get_triggers(_: Principal = Depends(require_agent_operator)) -> dict[str, object]:
    """Whether the runner reacts to the repository, and what it has done."""
    control = await controls.current()
    status = triggers.poller.status(control.max_parallel)
    status["active_sessions"] = await triggers.active_trigger_sessions()
    return status


# --- workspaces -----------------------------------------------------------


@app.get("/repositories", response_model=list[CodingRepository], tags=["workspaces"])
async def list_coding_repositories(_: Principal = Depends(require_agent_operator)) -> list[CodingRepository]:
    """Linked team repositories a session can be started for."""
    return [CodingRepository(**_coding_repository_fields(row)) for row in await db.enabled_coding_repositories()]


def _coding_repository_fields(row: dict) -> dict:
    hosts = row.get("agent_egress_hosts") or []
    if isinstance(hosts, str):
        hosts = json.loads(hosts)
    return {
        "id": int(row["id"]),
        "team_id": int(row["team_id"]),
        "repo_slug": str(row["repo_slug"]),
        "branch": str(row.get("branch") or "main"),
        "verification_command": row.get("verification_command"),
        "agent_egress_hosts": [str(host) for host in hosts],
        "agent_egress_unrestricted": bool(row.get("agent_egress_unrestricted")),
        "agent_may_comment": bool(row.get("agent_may_comment")),
    }


@app.get("/workspaces", response_model=list[Workspace], tags=["workspaces"])
async def list_workspaces(_: Principal = Depends(require_agent_operator)) -> list[Workspace]:
    return [Workspace(**row) for row in await db.list_workspaces()]


@app.get("/workspace-storage", response_model=WorkspaceStorage, tags=["workspaces"])
async def get_workspace_storage(_: Principal = Depends(require_agent_operator)) -> WorkspaceStorage:
    return await workspace_storage.read(await db.list_workspaces())


@app.post(
    "/workspaces",
    response_model=Workspace,
    status_code=status.HTTP_201_CREATED,
    tags=["workspaces"],
)
async def create_workspace(body: WorkspaceCreate, principal: Principal = Depends(require_agent_operator)) -> Workspace:
    try:
        row = await db.create_workspace(body.name, body.base_branch, principal.username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    await docker_engine.ensure_volume(row["volume_name"], labels={"logos.agent.workspace": row["name"]})
    return Workspace(**row, active_sessions=0)


@app.delete(
    "/workspaces/{workspace_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # 204 must carry no body, so the route returns a bare Response. Annotating
    # it `-> None` instead makes FastAPI derive a response model and refuse to
    # start the application.
    response_class=Response,
    tags=["workspaces"],
)
async def delete_workspace(workspace_id: int, _: Principal = Depends(require_agent_operator)) -> Response:
    workspace = await db.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    try:
        await db.delete_workspace(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    await docker_engine.remove_volume(workspace["volume_name"], force=True)
    await docker_engine.remove_volume(cache_volume_for(workspace), force=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- sessions -------------------------------------------------------------


@app.get("/sessions", response_model=list[SessionSummary], tags=["sessions"])
async def list_sessions(
    session_status: str | None = Query(default=None, alias="status"),
    workspace_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    _: Principal = Depends(require_agent_operator),
) -> list[SessionSummary]:
    rows = await db.list_sessions(status=session_status, workspace_id=workspace_id, limit=limit)
    return [SessionSummary(**_summary_fields(row)) for row in rows]


def _validate_memory_override(memory_mb: int | None) -> None:
    if memory_mb is None:
        return
    ceiling = memory_budget.override_ceiling_mb()
    if ceiling is None:
        raise HTTPException(409, "Custom RAM limits require a known runner memory budget.")
    if memory_mb > ceiling:
        raise HTTPException(422, f"Custom RAM limit must not exceed {ceiling} MiB on this runner.")


@app.post(
    "/sessions",
    response_model=SessionSummary,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["sessions"],
)
async def create_session(body: SessionCreate, principal: Principal = Depends(require_agent_operator)) -> SessionSummary:
    if body.deploy_to_dev:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent sessions no longer deploy to Dev automatically. Use the separate Dev deployment workflow.",
        )
    _validate_memory_override(body.memory_override_mb)
    workspace = await db.get_workspace(body.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")
    if body.no_push and (body.open_pull_request or body.screenshot_paths):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Private sessions cannot open a pull request or capture Dev screenshots",
        )
    # Refuse a model that is not served locally at the point where a person
    # asks for it, rather than accepting the session and failing it later:
    # agent work must never bill a cloud provider, and the operator finds out
    # in the form they submitted.
    policy = await model_policy.refresh()
    if not policy.allows(body.model):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=policy.refusal(body.model))

    task = body.task
    target: dict = {}
    if body.team_repository_id is not None:
        link = await db.coding_repository(body.team_repository_id)
        if link is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No linked repository with the coding agent enabled has this id",
            )
        if body.screenshot_paths:
            # The shared Dev instance belongs to the platform repository.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Dev screenshots are only available for the platform repository",
            )
        # The same task a triggered session for this repository gets: the
        # request, followed by the repository's build command and the
        # instructions its team wrote.
        with repository_context.scope(link):
            task = await conventions.for_task(body.task)
        target = {
            "repo_url": link["repo_url"],
            "repo_slug": link["repo_slug"],
            "team_repository_id": int(link["id"]),
            "verification_command": link.get("verification_command"),
            "repo_base_branch": link.get("branch") or "main",
        }
    try:
        session_id = await db.create_session(
            workspace_id=body.workspace_id,
            task=task,
            model=body.model,
            created_by=principal.username,
            open_pull_request=body.open_pull_request,
            no_push=body.no_push,
            deploy_to_dev=False,
            screenshot_paths=body.screenshot_paths,
            memory_override_mb=body.memory_override_mb,
            # An idle workspace that last worked on another repository moves
            # to this one's branch; a busy one is refused (db.WorkspaceBusy).
            point_workspace_at=target.get("repo_base_branch") or "main",
            **target,
        )
    except ValueError as exc:
        # The workspace was deleted between the 404 check above and the
        # insert: fail the create instead of accepting a session whose
        # workspace no longer exists.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    # Admission is the scheduler's job, but running a pass now means a session
    # created while the platform is idle starts in a second rather than at the
    # next tick.
    manager.request_scheduler_pass()

    row = await db.get_session(session_id)
    assert row is not None
    return SessionSummary(**_summary_fields(row))


@app.get("/sessions/{session_id}", response_model=SessionSummary, tags=["sessions"])
async def get_session(session_id: int, _: Principal = Depends(require_agent_operator)) -> SessionSummary:
    row = await db.get_session(session_id, include_pauses=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return SessionSummary(**_summary_fields(row))


@app.post("/sessions/{session_id}/cancel", tags=["sessions"])
async def cancel_session(session_id: int, _: Principal = Depends(require_agent_operator)) -> dict[str, object]:
    row = await db.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    cancelled = await manager.cancel(session_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is {row['status']} and cannot be cancelled",
        )
    return {"cancelled": True}


@app.post(
    "/sessions/{session_id}/retry",
    response_model=SessionSummary,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["sessions"],
)
async def retry_session(session_id: int, principal: Principal = Depends(require_agent_operator)) -> SessionSummary:
    """Queue the same work again, from where it came from.

    A session that failed keeps its task, its workspace and — for work that
    came from the repository — the branch and the thread it belongs to.
    Without this the only way back was to retype the task by hand, and a
    request the runner took on and then lost to an infrastructure failure
    was simply gone: the trigger counts as handled, so no later pass finds
    it again.

    A new row rather than a resurrection of the old one: the failed session
    keeps its transcript and its reason, which is the record of what
    happened.
    """
    row = await db.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if row["status"] not in {s.value for s in TERMINAL_STATUSES}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is {row['status']}; retry it once it has finished",
        )
    memory_override_mb = row.get("memory_override_mb")
    _validate_memory_override(memory_override_mb)
    try:
        new_id = await db.create_session(
            workspace_id=row["workspace_id"],
            task=row["task"],
            memory_override_mb=memory_override_mb,
            model=row.get("model"),
            created_by=principal.username or "operator",
            open_pull_request=bool(row.get("open_pull_request")),
            # Read-only is a property of the work, not the attempt: a
            # retried answer still must not push.
            no_push=bool(row.get("no_push")),
            # Keep the historical field false on every new attempt.
            deploy_to_dev=False,
            screenshot_paths=[],
            trigger_kind=row.get("trigger_kind"),
            trigger_ref=row.get("trigger_ref"),
            branch=row.get("branch_name"),
            reply_target=row.get("reply_target"),
            reaction_target=row.get("reaction_target"),
            priority=int(row.get("priority") or 50),
            priority_reason=row.get("priority_reason"),
            repo_url=row.get("repo_url"),
            repo_slug=row.get("repo_slug"),
            team_repository_id=row.get("team_repository_id"),
            verification_command=row.get("verification_command"),
            repo_base_branch=row.get("repo_base_branch"),
            automatic_analysis=False if row.get("trigger_kind") == "analysis" else None,
            retry_analysis_session_id=session_id if row.get("trigger_kind") == "analysis" else None,
            point_workspace_at=resumption_base(row),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.add_event(new_id, EventKind.STATUS, {"status": "queued", "retry_of": session_id})
    logger.info("session %s queued as a retry of %s", new_id, session_id)
    # Same reason as a fresh session: a pass now means it starts in a second
    # rather than at the next tick.
    manager.request_scheduler_pass()
    created = await db.get_session(new_id)
    assert created is not None
    return SessionSummary(**_summary_fields(created))


@app.post("/sessions/{session_id}/queue", response_model=SessionSummary, tags=["sessions"])
async def move_session_in_queue(
    session_id: int,
    body: QueueMove,
    principal: Principal = Depends(require_agent_operator),
) -> SessionSummary:
    """Move a queued session up, down, or to the front.

    Priority is derived from what a request is — a review outranks a fresh
    issue, a security label outranks a typo — and that is right most of the
    time. An operator watching the backlog knows the rest: which review is
    holding up a release, which issue can wait until tomorrow.
    """
    try:
        moved = await db.move_in_queue(session_id, body.move, by=principal.username or "an operator")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if moved is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a queued session can be moved in the queue",
        )
    logger.info("session %s moved %s in the queue by %s", session_id, body.move, principal.username)
    return SessionSummary(**_summary_fields(moved))


@app.get("/sessions/{session_id}/messages", response_model=SessionMessages, tags=["sessions"])
async def session_messages(
    session_id: int, request: Request, response: Response, _: Principal = Depends(require_agent_operator)
) -> SessionMessages | Response:
    if await db.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        data = await asyncio.to_thread(read_artifact, artifact_dir(session_id), "messages.json", limit=512 * 1024)
    except FileNotFoundError:
        return SessionMessages()
    except OSError:
        raise HTTPException(status_code=503, detail="Session messages could not be read")
    etag = '"' + hashlib.sha256(data).hexdigest() + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    try:
        messages = SessionMessages.model_validate_json(data, strict=True)
    except ValueError:
        raise HTTPException(status_code=503, detail="Session messages could not be read")
    response.headers.update(headers)
    return messages


@app.get("/sessions/{session_id}/events", response_model=list[SessionEvent], tags=["sessions"])
async def list_events(
    session_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
    _: Principal = Depends(require_agent_operator),
) -> list[SessionEvent]:
    if await db.get_session(session_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return [SessionEvent(**row) for row in await db.list_events(session_id, after_id=after_id, limit=limit)]


@app.get("/sessions/{session_id}/stream", tags=["sessions"])
async def stream_events(
    session_id: int,
    after_id: int = Query(default=0, ge=0),
    _: Principal = Depends(require_agent_operator),
) -> StreamingResponse:
    """Server-sent events: the session's event log as it is written.

    Polling the database rather than tailing the container directly means a
    reconnecting browser resumes exactly where it left off, and several viewers
    can watch the same session without multiplying load on the daemon.
    """
    if await db.get_session(session_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    async def generate():
        # Registered for as long as this response lives, so the write side
        # has somebody to nudge — and so nothing is left behind when the
        # browser disconnects mid-stream, which is the ordinary ending.
        async with pulse.watching(session_id):
            cursor = after_id
            idle_ticks = 0
            while True:
                events = await db.list_events(session_id, after_id=cursor, limit=200)
                for event in events:
                    cursor = event["id"]
                    payload = {
                        "id": event["id"],
                        "ts": event["ts"].isoformat(),
                        "kind": event["kind"],
                        "payload": event["payload"],
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                if events:
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    # Keep the connection alive through proxies during quiet spells.
                    yield ": keep-alive\n\n"

                session = await db.get_session(session_id)
                if session and session["status"] in (
                    SessionStatus.SUCCEEDED,
                    SessionStatus.FAILED,
                    SessionStatus.CANCELLED,
                ):
                    # Drain whatever landed after the terminal transition, then end
                    # the stream so the browser stops reconnecting.
                    remaining = await db.list_events(session_id, after_id=cursor, limit=200)
                    for event in remaining:
                        payload = {
                            "id": event["id"],
                            "ts": event["ts"].isoformat(),
                            "kind": event["kind"],
                            "payload": event["payload"],
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                    yield "event: end\ndata: {}\n\n"
                    return
                if idle_ticks > 600:  # ~20 minutes of nothing; let the client reconnect
                    return
                # Woken by the write side rather than by the clock: watching an
                # agent work is the one thing here that a person does in real
                # time, and a fixed tick puts a floor under how live it can be.
                # The timeout is the fallback, not the mechanism.
                await pulse.wait(session_id, timeout=2.0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _serve_session_file(directory: Path, relative: Path, limit: int) -> Response | None:
    """Serve a bounded artifact without following agent-written links."""
    try:
        data = read_artifact(directory, relative, limit=limit)
    except OSError:
        return None
    return StreamingResponse(
        iter([data]),
        media_type=mimetypes.guess_type(relative.name)[0] or "application/octet-stream",
        headers={"Content-Length": str(len(data))},
    )


@app.get("/sessions/{session_id}/screenshots/{name}", tags=["sessions"])
async def get_screenshot(session_id: int, name: str, _: Principal = Depends(require_agent_operator)) -> Response:
    # Reject traversal before touching the filesystem: `name` comes straight
    # from a URL, and the artefact root holds every session's output.
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid name")
    response = _serve_session_file(artifact_dir(session_id), Path("screenshots") / name, settings.screenshot_max_bytes)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screenshot not found")
    return response


@app.get("/sessions/{session_id}/pull-request", tags=["sessions"])
async def session_pull_request(session_id: int, _: Principal = Depends(require_agent_operator)) -> dict[str, object]:
    row = await db.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if not row.get("pr_url"):
        return {"pull_request": None}
    return {"pull_request": await github.pull_request_state(str(row["pr_url"]))}


def _summary_fields(row: dict) -> dict:
    """Project a database row onto the response model's fields."""
    keys = set(SessionSummary.model_fields)
    fields = {k: v for k, v in row.items() if k in keys}
    live = memory_usage.current(row["id"], row["status"])
    fields["memory_current_bytes"] = live
    if live is not None:
        # The sampler may commit a newer peak after this request's SELECT.
        fields["memory_peak_bytes"] = max(live, row.get("memory_peak_bytes") or 0)
    return fields


@app.post("/security/scans/trivy", tags=["security"])
async def import_security_scan(
    body: security_scans.ScanInput, principal: Principal = Depends(require_agent_operator)
) -> dict:
    try:
        return await security_scans.ingest_scan(body, principal.username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
