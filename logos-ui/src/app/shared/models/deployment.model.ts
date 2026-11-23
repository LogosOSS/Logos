/** How a deployment plan stands. A plan runs until every step has ended. */
export type PlanStatus = 'running' | 'succeeded' | 'failed' | 'rolled_back';

/**
 * What Logos knows about the work of one deployer. `waiting`: the core has to end first. `ordered`: released, and the
 * deployer has not taken it yet. `cancelled`: it never started, because the core failed or the plan was abandoned.
 */
export type StepStatus =
  | 'waiting'
  | 'ordered'
  | 'running'
  | 'succeeded'
  | 'unchanged'
  | 'rolled_back'
  | 'failed'
  | 'cancelled';

export type DeployerKind = 'core' | 'worker';

export interface StepBrief {
  name: string;
  kind: DeployerKind;
  status: StepStatus;
}

export interface DeploymentEvent {
  at: string;
  phase: string;
  note: string;
}

export interface StepDetails {
  drained_requests?: number;
  drain_seconds?: number;
  sole_replica_models?: string[];
}

export interface DeploymentStep {
  id: string;
  position: number;
  kind: DeployerKind;
  deployer_name: string;
  status: StepStatus;
  phase: string | null;
  failure: string | null;
  message: string | null;
  started_at: string | null;
  finished_at: string | null;
  last_activity_at: string;
  events: DeploymentEvent[];
  failure_logs: string | null;
  details: StepDetails;
}

export interface DeploymentPlan {
  id: string;
  /** The number of the deployment on its Logos, as the page shows it: #142. */
  number: number;
  tag: string;
  status: PlanStatus;
  initiator: string;
  reason: string | null;
  abandoned_by: string | null;
  created_at: string;
  finished_at: string | null;
  last_activity_at: string;
  /** One dot per deployer, for the history. */
  steps: StepBrief[];
  /** The full steps. Only the detail view of a plan has them. */
  step_details: DeploymentStep[] | null;
}

export interface Deployer {
  id: string;
  name: string;
  kind: DeployerKind;
  /** Seen recently. The button to deploy is off while the core deployer is not online. */
  online: boolean;
  last_seen_at: string | null;
  version: string | null;
  created_at: string;
}

/** The answer to making a deployer or replacing its key. The key is shown once and cannot be read again. */
export interface DeployerCreated {
  deployer: Deployer;
  key: string;
}

export interface DeploymentOverview {
  /**
   * A protected Logos (Prod) shows a warning, and a dialog asks the admin to confirm before it starts a deployment.
   * False on a Logos that needs no confirmation (Dev and Test).
   */
  requires_confirmation: boolean;
  deployers: Deployer[];
  can_start: boolean;
  /** Why a deployment cannot start now, in a sentence. */
  blocked_reason: string | null;
  suggested_tags: string[];
}

export const PLAN_STATUS_LABEL: Record<PlanStatus, string> = {
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  rolled_back: 'Rolled back',
};

export const STEP_STATUS_LABEL: Record<StepStatus, string> = {
  waiting: 'Waiting for the core',
  ordered: 'Waiting for the deployer',
  running: 'Running',
  succeeded: 'Succeeded',
  unchanged: 'Unchanged',
  rolled_back: 'Rolled back',
  failed: 'Failed',
  cancelled: 'Not started',
};

/** The tone of a status in the page: one colour for each of the four plan states, grey for what did not run. */
export type StatusTone = 'running' | 'succeeded' | 'failed' | 'rolled-back' | 'idle';

export function stepTone(status: StepStatus): StatusTone {
  switch (status) {
    case 'running':
      return 'running';
    case 'succeeded':
    case 'unchanged':
      return 'succeeded';
    case 'failed':
      return 'failed';
    case 'rolled_back':
      return 'rolled-back';
    default:
      return 'idle';
  }
}

export function planTone(status: PlanStatus): StatusTone {
  return status === 'rolled_back' ? 'rolled-back' : status;
}

// ── What the page draws from a step ───────────────────────────────────────

/** The four segments of the phase bar. The drain passes at once on a core host, and the segment stays done. */
export const PHASE_NAMES = ['Pull', 'Drain', 'Swap', 'Verify'] as const;

export type PhaseState = 'done' | 'active' | 'pending' | 'failed' | 'rolled_back';

const PHASE_INDEX: Record<string, number> = {
  queued: 0,
  pulling: 0,
  draining: 1,
  swapping: 2,
  verifying: 3,
  rolling_back: 3,
};

/** The segment in which a failure happened, by the kind of failure that the deployer reports. */
const FAILURE_INDEX: Record<string, number> = {
  pull_failed: 0,
  drain_timeout: 1,
  drain_blocked: 1,
  drain_unknown: 1,
  swap_failed: 2,
  verify_failed: 3,
  rollback_failed: 3,
};

type PhaseInput = Pick<DeploymentStep, 'status' | 'phase' | 'failure'>;

/** The state of each segment of the phase bar for a step. */
export function phaseStates(step: PhaseInput): PhaseState[] {
  const at = (phase: string | null) => PHASE_INDEX[phase ?? ''] ?? 0;
  const upTo = (index: number, state: PhaseState): PhaseState[] =>
    PHASE_NAMES.map((_, i) => (i < index ? 'done' : i === index ? state : 'pending'));
  switch (step.status) {
    case 'succeeded':
    case 'unchanged':
      return PHASE_NAMES.map(() => 'done');
    case 'running':
      return step.phase === 'rolling_back' ? upTo(3, 'rolled_back') : upTo(at(step.phase), 'active');
    case 'failed':
      return upTo(FAILURE_INDEX[step.failure ?? ''] ?? at(step.phase), 'failed');
    case 'rolled_back':
      return upTo(FAILURE_INDEX[step.failure ?? ''] ?? 3, 'failed');
    default:
      return PHASE_NAMES.map(() => 'pending');
  }
}

/** The kind of a node as the page says it. */
export function nodeKindLabel(kind: DeployerKind): string {
  return kind === 'core' ? 'Core node' : 'GPU worker';
}

/** The first line of a text, for a status line that must stay short. */
export function firstLine(text: string | null | undefined): string {
  return (text ?? '').trim().split('\n')[0].trim();
}

const DRAIN_NOTE = /^waiting for the node to drain: (\d+) request\(s\)/i;

/**
 * What a node is doing, in a few words: the status line of a node row. While a node drains, the line is cut to the
 * number of requests that are left, on a second line. The page of the deployment has the full message in its events.
 */
export function stepStatusText(step: DeploymentStep, tag: string): string {
  switch (step.status) {
    case 'running': {
      const note = firstLine(step.events[step.events.length - 1]?.note);
      const drain = DRAIN_NOTE.exec(note);
      if (drain) return `Waiting for the node to drain:\n${drain[1]} request(s)`;
      return note || STEP_STATUS_LABEL.running;
    }
    case 'succeeded':
      return `Done, runs ${tag}`;
    case 'unchanged':
      return `Unchanged, already runs ${tag}`;
    case 'failed':
    case 'rolled_back':
      return firstLine(step.message) || STEP_STATUS_LABEL[step.status];
    default:
      return STEP_STATUS_LABEL[step.status];
  }
}

/** "Oct 31, 2026, 1:58:00 PM": the full time for the title of a relative time. */
export function fullDate(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString('en-US', {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
        second: '2-digit',
      });
}

/** "13:41:07" in the local time, for the timeline of a node. */
export function clockTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('en-GB');
}

/** The title of an event in the timeline of a node. */
export function eventTitle(phase: string, status: StepStatus): string {
  switch (phase) {
    case 'queued':
      return 'Queued';
    case 'pulling':
      return 'Pull';
    case 'draining':
      return 'Drain';
    case 'swapping':
      return 'Swap';
    case 'verifying':
      return 'Verify';
    case 'rolling_back':
      return 'Rolling back';
    case 'done':
      return STEP_STATUS_LABEL[status];
    default:
      return phase;
  }
}
