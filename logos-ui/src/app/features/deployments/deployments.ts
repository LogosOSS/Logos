import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { DeploymentService } from '../../core/services/deployment.service';
import {
  Deployer,
  DeployerCreated,
  DeployerKind,
  DeploymentOverview,
  DeploymentPlan,
  DeploymentStep,
  PLAN_STATUS_LABEL,
  PlanStatus,
  STEP_STATUS_LABEL,
  planTone,
  stepTone,
} from '../../shared/models/deployment.model';
import { formatRelativeTime } from '../../shared/utils/date';
import { errorDetail } from '../../shared/utils/error-detail';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { SelectComponent, AppSelectOption } from '../../shared/components/select/select';

/** How often the page reads the state again. A deployment takes minutes, so a few seconds is soon enough. */
const POLL_MS = 5000;

/** "3 min 20 s", "45 s" or "2 h 5 min": how long a step took. */
export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

@Component({
  selector: 'app-deployments',
  standalone: true,
  imports: [CommonModule, FormsModule, SelectComponent, ModalConfirmComponent],
  templateUrl: './deployments.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './deployments.scss',
})
export class Deployments implements OnInit {
  private service = inject(DeploymentService);
  private route = inject(ActivatedRoute);
  private destroyRef = inject(DestroyRef);

  readonly planLabel = PLAN_STATUS_LABEL;
  readonly stepLabel = STEP_STATUS_LABEL;
  readonly planTone = planTone;
  readonly stepTone = stepTone;
  readonly age = formatRelativeTime;
  readonly formatDuration = formatDuration;

  readonly kindOptions: AppSelectOption[] = [
    { value: 'worker', label: 'Worker' },
    { value: 'core', label: 'Core' },
  ];
  readonly statusChips: { value: 'all' | PlanStatus; label: string }[] = [
    { value: 'all', label: 'All' },
    { value: 'running', label: PLAN_STATUS_LABEL.running },
    { value: 'succeeded', label: PLAN_STATUS_LABEL.succeeded },
    { value: 'failed', label: PLAN_STATUS_LABEL.failed },
    { value: 'rolled_back', label: PLAN_STATUS_LABEL.rolled_back },
  ];

  // ── data ─────────────────────────────────────────────────────────────────
  overview = signal<DeploymentOverview | null>(null);
  plans = signal<DeploymentPlan[]>([]);
  selectedEnv = signal('');
  statusFilter = signal<'all' | PlanStatus>('all');
  openPlan = signal<DeploymentPlan | null>(null);
  loading = signal(true);
  loadError = signal<string | null>(null);

  // ── starting a deployment ────────────────────────────────────────────────
  tag = signal('latest');
  confirmation = signal('');
  confirmOpen = signal(false);
  starting = signal(false);
  startError = signal<string | null>(null);

  // ── abandoning ───────────────────────────────────────────────────────────
  abandonOpen = signal(false);
  abandoning = signal(false);
  abandonError = signal<string | null>(null);

  // ── deployers ────────────────────────────────────────────────────────────
  addOpen = signal(false);
  newName = signal('');
  newKind = signal<DeployerKind>('worker');
  creating = signal(false);
  createError = signal<string | null>(null);
  /** The key of a deployer that was just made or given a new key. It is shown once. */
  createdKey = signal<DeployerCreated | null>(null);
  keyCopied = signal(false);
  removeTarget = signal<Deployer | null>(null);
  replaceTarget = signal<Deployer | null>(null);
  deployerBusy = signal(false);
  deployerError = signal<string | null>(null);

  // ── derived ──────────────────────────────────────────────────────────────
  target = computed(() => this.overview()?.targets.find((t) => t.name === this.selectedEnv()) ?? null);
  currentPlan = computed(() => this.plans().find((p) => p.status === 'running') ?? null);
  history = computed(() => {
    const filter = this.statusFilter();
    return this.plans().filter((p) => filter === 'all' || p.status === filter);
  });
  /** The plan the detail view shows, kept current when the list is read again. */
  openDetail = computed(() => this.openPlan());
  /** Whether a plan that this Logos started runs in the open environment, and so can be abandoned. */
  canAbandon = (plan: DeploymentPlan | null) => !!plan && plan.status === 'running' && plan.origin === 'local';

  ngOnInit(): void {
    void this.load(true);
    const timer = setInterval(() => void this.load(false), POLL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  // ── loading ──────────────────────────────────────────────────────────────
  async load(initial: boolean): Promise<void> {
    try {
      const overview = await this.service.getOverview();
      this.overview.set(overview);
      if (overview.enabled) {
        const names = overview.targets.map((t) => t.name);
        if (!names.includes(this.selectedEnv())) {
          this.selectedEnv.set(overview.environment && names.includes(overview.environment) ? overview.environment : (names[0] ?? ''));
        }
        await this.loadPlans();
        if (initial) await this.openFromLink();
      }
      this.loadError.set(null);
    } catch (e) {
      if (initial || this.overview() === null) {
        this.loadError.set(errorDetail(e) ?? 'The deployments could not be loaded.');
      }
    } finally {
      this.loading.set(false);
    }
  }

  private async loadPlans(): Promise<void> {
    const env = this.selectedEnv();
    const plans = await this.service.getPlans(env, 50);
    if (env !== this.selectedEnv()) return;
    this.plans.set(plans);
    const open = this.openPlan();
    if (open && open.environment === env) {
      const fresh = await this.service.getPlan(open.id).catch(() => null);
      if (fresh && this.openPlan()?.id === fresh.id) this.openPlan.set(fresh);
    }
  }

  /** A notification links to `?plan=<id>`: show that plan. */
  private async openFromLink(): Promise<void> {
    const id = this.route.snapshot.queryParamMap.get('plan');
    if (!id || !UUID.test(id)) return;
    const plan = await this.service.getPlan(id).catch(() => null);
    if (!plan) return;
    if (this.overview()?.targets.some((t) => t.name === plan.environment)) {
      this.selectedEnv.set(plan.environment);
      await this.loadPlans();
    }
    this.openPlan.set(plan);
  }

  async selectEnv(name: string): Promise<void> {
    if (name === this.selectedEnv()) return;
    this.selectedEnv.set(name);
    this.openPlan.set(null);
    this.plans.set([]);
    this.startError.set(null);
    try {
      await this.loadPlans();
    } catch (e) {
      this.loadError.set(errorDetail(e) ?? 'The deployments could not be loaded.');
    }
  }

  async open(plan: DeploymentPlan): Promise<void> {
    this.openPlan.set(plan);
    const detail = await this.service.getPlan(plan.id).catch(() => null);
    if (detail && this.openPlan()?.id === plan.id) this.openPlan.set(detail);
  }

  close(): void {
    this.openPlan.set(null);
  }

  // ── starting ─────────────────────────────────────────────────────────────
  /** Dev and Test start at once. A protected environment (Prod) first asks for its typed name. */
  deploy(): void {
    this.startError.set(null);
    const target = this.target();
    if (!target || !this.tag().trim()) {
      this.startError.set('Enter an image tag.');
      return;
    }
    if (target.requires_confirmation) {
      this.confirmation.set('');
      this.confirmOpen.set(true);
      return;
    }
    void this.start();
  }

  confirmStart(): void {
    const env = this.selectedEnv();
    if (this.confirmation().trim().toLowerCase() !== env.toLowerCase()) {
      this.startError.set(`Type ${env.toLowerCase()} to confirm.`);
      return;
    }
    void this.start();
  }

  closeConfirm(): void {
    this.confirmOpen.set(false);
    this.startError.set(null);
  }

  private async start(): Promise<void> {
    this.starting.set(true);
    try {
      const plan = await this.service.startPlan(
        this.selectedEnv(),
        this.tag().trim(),
        this.target()?.requires_confirmation ? this.confirmation().trim() : undefined,
      );
      this.confirmOpen.set(false);
      this.openPlan.set(plan);
      await this.load(false);
    } catch (e) {
      this.startError.set(errorDetail(e) ?? 'The deployment could not be started.');
    } finally {
      this.starting.set(false);
    }
  }

  // ── abandoning ───────────────────────────────────────────────────────────
  askAbandon(): void {
    this.abandonError.set(null);
    this.abandonOpen.set(true);
  }

  closeAbandon(): void {
    this.abandonOpen.set(false);
  }

  async confirmAbandon(): Promise<void> {
    const plan = this.currentPlan() ?? this.openPlan();
    if (!plan) return;
    this.abandoning.set(true);
    try {
      const updated = await this.service.abandonPlan(plan.id);
      this.abandonOpen.set(false);
      if (this.openPlan()?.id === plan.id) this.openPlan.set(updated);
      await this.load(false);
    } catch (e) {
      this.abandonError.set(errorDetail(e) ?? 'The deployment could not be abandoned.');
    } finally {
      this.abandoning.set(false);
    }
  }

  // ── deployers ────────────────────────────────────────────────────────────
  toggleAdd(): void {
    this.addOpen.update((open) => !open);
    this.createError.set(null);
  }

  async createDeployer(): Promise<void> {
    const name = this.newName().trim();
    if (!name) {
      this.createError.set('Enter a name for the deployer, for example the name of its host.');
      return;
    }
    this.creating.set(true);
    this.createError.set(null);
    try {
      const created = await this.service.createDeployer(name, this.selectedEnv(), this.newKind());
      this.createdKey.set(created);
      this.keyCopied.set(false);
      this.addOpen.set(false);
      this.newName.set('');
      await this.load(false);
    } catch (e) {
      this.createError.set(errorDetail(e) ?? 'The deployer could not be made.');
    } finally {
      this.creating.set(false);
    }
  }

  async confirmReplaceKey(): Promise<void> {
    const deployer = this.replaceTarget();
    if (!deployer) return;
    this.deployerBusy.set(true);
    this.deployerError.set(null);
    try {
      this.createdKey.set(await this.service.replaceDeployerKey(deployer.id));
      this.keyCopied.set(false);
      this.replaceTarget.set(null);
      await this.load(false);
    } catch (e) {
      this.deployerError.set(errorDetail(e) ?? 'The key could not be replaced.');
    } finally {
      this.deployerBusy.set(false);
    }
  }

  async confirmRemove(): Promise<void> {
    const deployer = this.removeTarget();
    if (!deployer) return;
    this.deployerBusy.set(true);
    this.deployerError.set(null);
    try {
      await this.service.deleteDeployer(deployer.id);
      this.removeTarget.set(null);
      await this.load(false);
    } catch (e) {
      this.deployerError.set(errorDetail(e) ?? 'The deployer could not be removed.');
    } finally {
      this.deployerBusy.set(false);
    }
  }

  closeKey(): void {
    this.createdKey.set(null);
  }

  async copyKey(): Promise<void> {
    const key = this.createdKey()?.key;
    if (!key) return;
    try {
      await navigator.clipboard.writeText(key);
      this.keyCopied.set(true);
    } catch {
      this.keyCopied.set(false);
    }
  }

  // ── view helpers ─────────────────────────────────────────────────────────
  /** How long a step took, or how long it has run. Empty before it started. */
  stepDuration(step: DeploymentStep): string {
    if (!step.started_at) return '';
    const start = new Date(step.started_at).getTime();
    const end = step.finished_at ? new Date(step.finished_at).getTime() : Date.now();
    return Number.isNaN(start) || Number.isNaN(end) ? '' : formatDuration(end - start);
  }

  /** The dot of an environment in the switch: its core deployers are all online, or something needs a look. */
  envState(name: string): 'ok' | 'warn' | 'idle' {
    const target = this.overview()?.targets.find((t) => t.name === name);
    if (!target || target.deployers.length === 0) return 'idle';
    const cores = target.deployers.filter((d) => d.kind === 'core');
    if (cores.length > 0 && !cores.every((d) => d.online)) return 'warn';
    return 'ok';
  }

  stepSummary(step: { name: string; kind: string; status: string }): string {
    return `${step.name} (${step.kind}): ${step.status.replace('_', ' ')}`;
  }
}
