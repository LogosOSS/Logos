export type AgentSessionStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'paused'
  | 'finalizing'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

export type AgentEventKind =
  'log' | 'status' | 'pull_request' | 'deploy' | 'screenshot' | 'capacity' | 'error';

export interface AgentMessage {
  id: string;
  actor: string;
  text: string;
  truncated: boolean;
}

export interface SessionMessages {
  messages: AgentMessage[];
  omitted: number;
}

export interface AgentWorkspace {
  id: number;
  name: string;
  base_branch: string;
  volume_name: string;
  created_by: string;
  created_at: string;
  active_sessions: number;
}

export interface AgentWorkspaceStorage {
  artifact_filesystem: { total_bytes: number; available_bytes: number } | null;
  workspace_used_bytes: Record<number, number | null>;
}

export interface AgentSession {
  id: number;
  workspace_id: number;
  workspace_name: string;
  task: string;
  status: AgentSessionStatus;
  model: string | null;
  branch_name: string | null;
  pr_url: string | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: number | null;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
  /** What the agent reported spending, in US dollars. */
  cost_usd: number;
  memory_mb?: number | null;
  memory_override_mb?: number | null;
  memory_current_bytes?: number | null;
  memory_peak_bytes?: number | null;
  subagent_count?: number | null;
  progress_percent?: number | null;
  progress_active_s?: number | null;
  progress_reported_at?: string | null;
  active_seconds?: number;
  active_since?: string | null;
  screenshot_count: number;
  /** Set when the runner queued this session itself: 'issue' or 'review'. */
  trigger_kind: string | null;
  /** Which event it reacted to, e.g. 'issue'.*/
  trigger_ref: string | null;
  /**
   * The linked team repository (owner/name) the session works on or
   * analyses; null for a session on the platform repository.
   */
  repo_slug?: string | null;
  /** For a repository analysis: the team that linked the repository. */
  team_name?: string | null;
  /** How urgent the work is (higher runs first), and why. */
  priority: number;
  priority_reason: string | null;
  /**
   * The environment notes this session was handed, as they stood when it
   * started. Null for a session that never started and for ones that ran
   * before the runner kept a copy.
   */
  environment_notes: string | null;
}

/** The standing text every session is given, and whether it is the default. */
export interface AgentInstructions {
  house_rules: string;
  environment_notes: string;
  house_rules_default: boolean;
  environment_notes_default: boolean;
  updated_by: string;
}

export interface AgentEvent {
  id: number;
  session_id: number;
  ts: string;
  kind: AgentEventKind;
  payload: Record<string, unknown>;
}

/** What the runner currently believes about spare serving capacity. */
export interface AgentCapacity {
  load: number;
  total_slots: number;
  busy_slots: number;
  /** Slots held by this runner (sessions and their subagents). */
  own_slots: number;
  /** Slots held by other production traffic on the same local model. */
  other_slots: number;
  sessions_running: number;
  sessions_queued: number;
  sessions_paused: number;
  max_parallel: number;
  may_start: boolean;
  reason: string;
  /** False when the agent key could reach a cloud model: nothing starts. */
  models_local_only: boolean;
  models_detail: string;
}

/** What an operator has changed about the runner while it runs. */
export type AgentRunnerMode = 'running' | 'draining' | 'paused';

export interface AgentControls {
  /** running: as configured · draining: nothing new · paused: hand it all back. */
  mode: AgentRunnerMode;
  mode_reason: string;
  paused: boolean;
  admits_new_sessions: boolean;
  /** The ceiling in force right now. */
  max_parallel: number;
  /** Null when the ceiling is the maximum the memory budget allows. */
  max_parallel_override: number | null;
  max_parallel_configured: number;
  /** The most sessions at once with the current memory per session. */
  max_parallel_available: number;
  /** The memory limit of a session started now, in MiB. */
  session_memory_mb: number;
  /** Null when the deployment's default memory per session applies. */
  session_memory_override: number | null;
  session_memory_configured: number;
  /** The memory all sessions together may use, in MiB; null without a budget. */
  memory_budget_mb: number | null;
  host_memory_mb: number | null;
  /** What running, paused and starting sessions have reserved, in MiB. */
  memory_reserved_mb: number;
  updated_by: string;
}

/** The locally served models a session may be driven by. */
export interface AgentModels {
  models: string[];
  default: string;
  local_only: boolean;
  detail: string;
}

/** Whether the runner reacts to the repository, and what it has queued. */
export interface AgentTriggers {
  enabled: boolean;
  polling: boolean;
  /** The GitHub account whose assignments and mentions it answers. */
  account: string;
  poll_interval_s: number;
  max_active_sessions: number;
  active_sessions: number;
  last_pass: string | null;
  queued_total: number;
  last_error: string;
}

export interface CreateSessionRequest {
  workspace_id: number;
  task: string;
  model?: string | null;
  memory_override_mb?: number;
  open_pull_request: boolean;
  no_push?: boolean;
  deploy_to_dev: boolean;
  screenshot_paths: string[];
  /** A linked team repository to work on; null works on the platform repository. */
  team_repository_id?: number | null;
}

/** A linked team repository with the coding agent enabled. */
export interface AgentRepository {
  id: number;
  team_id: number;
  repo_slug: string;
  branch: string;
  verification_command: string | null;
  /** HTTPS hosts sessions for this repository may reach; empty means no internet. */
  agent_egress_hosts: string[];
  /** Whether sessions may reach any public HTTPS host instead of only the listed ones. */
  agent_egress_unrestricted: boolean;
  /** Whether the agent may comment on issues and pull requests during a session. */
  agent_may_comment: boolean;
}

/** Sessions in these states still hold a container and a workspace. */
export const ACTIVE_SESSION_STATUSES: readonly AgentSessionStatus[] = [
  'queued',
  'starting',
  'running',
  'paused',
  'finalizing',
];

export function isActive(status: AgentSessionStatus): boolean {
  return ACTIVE_SESSION_STATUSES.includes(status);
}
