"""Request and response models, and the session state machine.

The state machine is small enough to state in one place, and every transition
the service performs goes through :func:`can_transition` so an illegal one
fails loudly instead of leaving a row in a state the UI cannot render.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SessionStatus(StrEnum):
    QUEUED = "queued"  # accepted, waiting for capacity
    STARTING = "starting"  # container being created
    RUNNING = "running"  # agent working
    PAUSED = "paused"  # container stopped to give capacity back
    FINALIZING = "finalizing"  # agent exited, the trusted finalizer is pushing
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({SessionStatus.SUCCEEDED, SessionStatus.FAILED, SessionStatus.CANCELLED})
ACTIVE_STATUSES = frozenset(
    {
        SessionStatus.QUEUED,
        SessionStatus.STARTING,
        SessionStatus.RUNNING,
        SessionStatus.PAUSED,
        # Finalizing still occupies the workspace: the finalizer container
        # has the volume mounted and runs git in the working copy.
        SessionStatus.FINALIZING,
    }
)

# Transitions the service is allowed to make. Paused sessions return to
# running (their container is unpaused), never to starting: the container and
# its workspace survive the pause, so re-running setup would lose work.
_ALLOWED: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.QUEUED: frozenset({SessionStatus.STARTING, SessionStatus.CANCELLED, SessionStatus.FAILED}),
    SessionStatus.STARTING: frozenset({SessionStatus.RUNNING, SessionStatus.FAILED, SessionStatus.CANCELLED}),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.PAUSED,
            SessionStatus.FINALIZING,
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
    ),
    SessionStatus.PAUSED: frozenset({SessionStatus.RUNNING, SessionStatus.CANCELLED, SessionStatus.FAILED}),
    # Finalizing is not pausable and never returns to running: the agent
    # container is gone, so a pause would have nothing to freeze, and a
    # resume would hand the row back with no supervisor to finish it. The
    # finalizer runs to completion, or the session is cancelled or failed.
    SessionStatus.FINALIZING: frozenset({SessionStatus.SUCCEEDED, SessionStatus.FAILED, SessionStatus.CANCELLED}),
    SessionStatus.SUCCEEDED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.CANCELLED: frozenset(),
}


def can_transition(current: SessionStatus, target: SessionStatus) -> bool:
    return target in _ALLOWED[current]


class EventKind(StrEnum):
    LOG = "log"
    STATUS = "status"
    PULL_REQUEST = "pull_request"
    DEPLOY = "deploy"
    SCREENSHOT = "screenshot"
    CAPACITY = "capacity"
    ERROR = "error"


# --- workspaces -----------------------------------------------------------


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_branch: str = Field(default="main", max_length=128)

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        # The name becomes part of a Docker volume and a git branch, so keep it
        # to characters that are safe in both.
        cleaned = "".join(c if c.isalnum() or c in "-_" else "-" for c in v.strip().lower())
        if not cleaned.strip("-_"):
            raise ValueError("name must contain at least one alphanumeric character")
        return cleaned


class CodingRepository(BaseModel):
    """A linked team repository a session can be started for."""

    id: int
    team_id: int
    repo_slug: str
    branch: str
    verification_command: str | None = None
    agent_egress_hosts: list[str] = Field(default_factory=list)
    agent_egress_unrestricted: bool = False
    agent_may_comment: bool = False


class Workspace(BaseModel):
    id: int
    name: str
    base_branch: str
    volume_name: str
    created_by: str
    created_at: datetime
    active_sessions: int = 0


class ArtifactFilesystem(BaseModel):
    total_bytes: int = Field(gt=0)
    available_bytes: int = Field(ge=0)


class WorkspaceStorage(BaseModel):
    artifact_filesystem: ArtifactFilesystem | None = None
    workspace_used_bytes: dict[int, int | None] = Field(default_factory=dict)


# --- sessions -------------------------------------------------------------


class SessionCreate(BaseModel):
    workspace_id: int
    task: str = Field(min_length=8, max_length=8000)
    # Which Logos-served model drives the agent. Absent means the runner picks
    # the configured default.
    model: str | None = Field(default=None, max_length=200)
    # Open a pull request when the work finishes and the diff is non-empty.
    open_pull_request: bool = True
    # Manual private work may read its repository but must not publish to GitHub.
    no_push: bool = False
    # Retained for old clients. The endpoint rejects true and accepts false.
    deploy_to_dev: bool = False
    # Paths to capture on the Dev instance already running.
    screenshot_paths: list[str] = Field(default_factory=list, max_length=10)
    # A linked team repository with coding work enabled to work on instead
    # of the platform's own repository. Absent means the platform repository.
    team_repository_id: int | None = Field(default=None, ge=1)

    # A manual session may request a limit in MiB without changing the runner default.
    memory_override_mb: int | None = Field(default=None, strict=True, ge=512, le=1_048_576)

    @field_validator("screenshot_paths")
    @classmethod
    def _relative_paths(cls, v: list[str]) -> list[str]:
        for path in v:
            if not path.startswith("/") or path.startswith("//"):
                raise ValueError(f"screenshot path must be an absolute path on the dev host: {path}")
        return v


class InferenceSettingsUpdate(BaseModel):
    mode: Literal["local", "cloud"]
    model: str | None = Field(default=None, min_length=1, max_length=255)
    provider_id: int | None = Field(default=None, strict=True, ge=1, le=2147483647)
    model_id: int | None = Field(default=None, strict=True, ge=1, le=2147483647)


class CloudInferenceOption(BaseModel):
    provider_id: int
    provider_name: str
    model_id: int
    model: str


class InferenceSettings(InferenceSettingsUpdate):
    local_models: list[str]
    cloud_options: list[CloudInferenceOption]
    cloud_selection_valid: bool = True


class SessionSummary(BaseModel):
    id: int
    workspace_id: int
    workspace_name: str
    task: str
    status: SessionStatus
    model: str | None
    inference_provider_id: int | None = None
    inference_model_id: int | None = None
    branch_name: str | None
    pr_url: str | None
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    exit_code: int | None
    error: str | None
    tokens_in: int
    tokens_out: int
    # What the agent reported spending, in US dollars — the currency
    # Claude Code reports. Authoritative accounting stays in the request log.
    cost_usd: float
    memory_mb: int | None = None  # Per-container limit at admission, in MiB.
    memory_override_mb: int | None = None
    memory_current_bytes: int | None = None
    memory_peak_bytes: int | None = None
    subagent_count: int | None = None
    progress_percent: int | None = None
    progress_active_s: int | None = None
    progress_reported_at: datetime | None = None
    active_seconds: float = 0
    active_since: datetime | None = None
    screenshot_count: int = 0
    # Set when the runner queued this session itself in response to the
    # repository: what kind of event it was ('issue', 'review') and which one
    # ('<event-kind>'). Null for sessions a person created.
    trigger_kind: str | None = None
    trigger_ref: str | None = None
    # For a repository analysis: which linked repository and whose team it
    # is, so the session list says what is being analysed. The slug stays
    # after the link is removed; the team name does not.
    repo_slug: str | None = None
    team_name: str | None = None
    # How urgent this work is, and the sentence explaining it.
    priority: int = 50
    priority_reason: str | None = None
    # The environment notes this session was handed, as they stood when it
    # started. Null for a session that never started, and for the ones that
    # ran before the runner kept a copy — the page says so rather than
    # showing today's text as if it were theirs.
    environment_notes: str | None = None


class InstructionState(BaseModel):
    """The standing text every session is given."""

    house_rules: str
    environment_notes: str
    # Whether each half is what the code ships with, or somebody's decision.
    house_rules_default: bool
    environment_notes_default: bool
    updated_by: str = ""


class InstructionUpdate(BaseModel):
    """New standing text.

    Each half is optional and left as it is stored when it is absent, so a
    page can save or reset one box without also submitting whatever is
    typed in the other. `reset_*` is the third state: back to the text the
    code ships with, which an empty string does not mean.
    """

    house_rules: str | None = Field(default=None, max_length=20000)
    environment_notes: str | None = Field(default=None, max_length=20000)
    reset_house_rules: bool = False
    reset_environment_notes: bool = False


class QueueMove(BaseModel):
    """Where an operator wants a queued session to sit.

    A move rather than a number: the order is what the runner works on
    while the platform is busy, and picking a number means guessing what
    the neighbours are.
    """

    move: str = Field(pattern="^(up|down|first)$")


class AssistantMessage(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    actor: str = Field(pattern=r"^(main|child-[a-f0-9]{8})$")
    text: str = Field(min_length=1, max_length=16384)
    truncated: bool = False


class SessionMessages(BaseModel):
    messages: list[AssistantMessage] = Field(default_factory=list, max_length=64)
    omitted: int = Field(default=0, ge=0)


class SessionEvent(BaseModel):
    id: int
    session_id: int
    ts: datetime
    kind: EventKind
    payload: dict


class ControlState(BaseModel):
    """What an operator has changed about the runner while it runs."""

    # 'running', 'draining' (start nothing new), or 'paused' (hand
    # everything back now).
    mode: str = "running"
    mode_reason: str = ""
    paused: bool = False
    admits_new_sessions: bool = True
    # The ceiling in force. `max_parallel_override` is null when the
    # environment's configured value stands.
    max_parallel: int
    max_parallel_override: int | None = None
    max_parallel_configured: int
    # The most sessions that may run at once with the current memory per
    # session: what the host's memory budget holds, within the configured
    # cap. The ceiling in force never exceeds it; an operator can lower it.
    max_parallel_available: int = 0
    # The memory limit of a session started now, and the deployment's default.
    session_memory_mb: int = 0
    session_memory_override: int | None = None
    session_memory_configured: int = 0
    # The memory all sessions together may claim, null without a budget, and
    # what the live sessions have reserved of it.
    memory_budget_mb: int | None = None
    host_memory_mb: int | None = None
    memory_reserved_mb: int = 0
    updated_by: str = ""


class ControlUpdate(BaseModel):
    """A change to the runtime controls. Omitted fields stay as they are."""

    # 'running' works as configured, 'draining' starts nothing new while
    # what runs finishes, 'paused' hands everything back now.
    mode: str | None = Field(default=None, pattern="^(running|draining|paused)$")
    # Why it was stopped, for the people who find it stopped.
    reason: str = Field(default="", max_length=200)
    # 0 drains without pausing: nothing new starts, what runs keeps running.
    # Null clears the override and the configured ceiling applies again.
    max_parallel: int | None = Field(default=None, ge=0, le=100)
    clear_max_parallel: bool = False
    # The memory limit of each session started from now on, in MiB. Null
    # with clear_session_memory_mb goes back to the deployment's default.
    session_memory_mb: int | None = Field(default=None, ge=512, le=1_048_576)
    clear_session_memory_mb: bool = False


class CapacityState(BaseModel):
    """What the runner currently believes about spare serving capacity."""

    load: float  # 0..1, reserved share of local serving slots
    total_slots: int
    busy_slots: int
    # How many of those busy slots this runner holds (sessions and the
    # subagents they started). The rest are other production traffic.
    own_slots: int = 0
    other_slots: int = 0
    sessions_running: int
    sessions_queued: int
    sessions_paused: int
    max_parallel: int
    may_start: bool
    reason: str
    remote_may_start: bool
    remote_reason: str
    # Whether every model the runner's key can reach is served locally. False
    # stops admission outright: agent work is paid for in idle GPU time, never
    # in cloud tokens.
    models_local_only: bool = False
    models_detail: str = ""
