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

/** `local`: an admin of this Logos started it. `observed`: deployers reported it, another Logos ordered it. */
export type PlanOrigin = 'local' | 'observed';

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
  environment: string;
  tag: string;
  status: PlanStatus;
  origin: PlanOrigin;
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
  environment: string;
  kind: DeployerKind;
  /** Seen recently. The button of an environment is off while its core deployer is not online. */
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

export interface DeploymentTarget {
  name: string;
  own: boolean;
  /** A protected environment (Prod) asks for its typed name and shows a warning first. */
  requires_confirmation: boolean;
  deployers: Deployer[];
  running_plan_id: string | null;
  can_start: boolean;
  /** Why a deployment cannot start now, in a sentence. */
  blocked_reason: string | null;
}

export interface DeploymentOverview {
  /** False on a Logos without deployers (for example one from the installation guide). */
  enabled: boolean;
  environment: string;
  targets: DeploymentTarget[];
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
