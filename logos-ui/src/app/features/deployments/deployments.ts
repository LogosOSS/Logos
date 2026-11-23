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
import { Router, RouterLink } from '@angular/router';
import { DeploymentService } from '../../core/services/deployment.service';
import {
  Deployer,
  DeployerCreated,
  DeploymentOverview,
  DeploymentPlan,
  PLAN_STATUS_LABEL,
  PlanStatus,
  STEP_STATUS_LABEL,
  fullDate,
  nodeKindLabel,
  phaseStates,
  planTone,
  stepStatusText,
  stepTone,
} from '../../shared/models/deployment.model';
import { formatRelativeTime } from '../../shared/utils/date';
import { errorDetail } from '../../shared/utils/error-detail';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { PhaseBar } from './phase-bar';
import { StatusPill } from './status-pill';

/** How often the page reads the state again. A deployment takes minutes, so a few seconds is soon enough. */
const POLL_MS = 5000;

@Component({
  selector: 'app-deployments',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterLink,
    ModalConfirmComponent,
    PhaseBar,
    StatusPill,
  ],
  templateUrl: './deployments.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./deployments-shared.scss', './deployments.scss'],
})
export class Deployments implements OnInit {
  private service = inject(DeploymentService);
  private router = inject(Router);
  private destroyRef = inject(DestroyRef);

  readonly planLabel = PLAN_STATUS_LABEL;
  readonly stepLabel = STEP_STATUS_LABEL;
  readonly planTone = planTone;
  readonly stepTone = stepTone;
  readonly phaseStates = phaseStates;
  readonly stepStatusText = stepStatusText;
  readonly nodeKindLabel = nodeKindLabel;
  readonly fullDate = fullDate;
  readonly age = formatRelativeTime;
  /** What a protected Logos tells the admin, in the banner and again in the dialog before a deployment starts. */
  readonly liveWarning =
    'This Logos serves real traffic. Each node leaves service while it drains and is replaced. ' +
    'A worker that fails its checks is rolled back. The core has no automatic rollback.';

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
  /** The steps of the running plan: the list has only one dot for each step. */
  runningDetail = signal<DeploymentPlan | null>(null);
  statusFilter = signal<'all' | PlanStatus>('all');
  loading = signal(true);
  loadError = signal<string | null>(null);

  // ── starting a deployment ────────────────────────────────────────────────
  tag = signal('latest');
  /** The dialog that a protected Logos shows before it starts a deployment. */
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
  currentPlan = computed(() => this.plans().find((p) => p.status === 'running') ?? null);
  lastPlan = computed(() => this.plans()[0] ?? null);
  history = computed(() => {
    const filter = this.statusFilter();
    return this.plans().filter((p) => filter === 'all' || p.status === filter);
  });
  canAbandon = (plan: DeploymentPlan | null) => !!plan && plan.status === 'running';
  /** What the removal dialog says. A worker comes back by itself as long as its node manager runs. */
  removeMessage = computed(() =>
    this.removeTarget()?.kind === 'worker'
      ? 'It shows up here again when its node manager asks Logos for orders. The history of its deployments stays.'
      : 'Its key stops working. The history of its deployments stays.',
  );

  ngOnInit(): void {
    void this.load(true);
    const timer = setInterval(() => void this.load(false), POLL_MS);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  // ── loading ──────────────────────────────────────────────────────────────
  async load(initial: boolean): Promise<void> {
    try {
      this.overview.set(await this.service.getOverview());
      await this.loadPlans();
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
    const plans = await this.service.getPlans(50);
    this.plans.set(plans);
    const running = plans.find((p) => p.status === 'running');
    this.runningDetail.set(running ? await this.service.getPlan(running.id).catch(() => null) : null);
  }

  // ── starting ─────────────────────────────────────────────────────────────
  /** The button Deploy. A protected Logos asks in a dialog first; the others start at once. */
  async deploy(): Promise<void> {
    this.startError.set(null);
    if (!this.tag().trim()) {
      this.startError.set('Enter an image tag.');
      return;
    }
    if (this.overview()?.requires_confirmation) {
      this.confirmOpen.set(true);
      return;
    }
    await this.start();
  }

  closeConfirm(): void {
    this.confirmOpen.set(false);
  }

  /** Start the deployment. The dialog calls this when the admin confirms. */
  async start(): Promise<void> {
    this.starting.set(true);
    try {
      const plan = await this.service.startPlan(this.tag().trim());
      await this.router.navigate(['/deployments', plan.id]);
    } catch (e) {
      this.startError.set(errorDetail(e) ?? 'The deployment could not be started.');
    } finally {
      this.starting.set(false);
      this.confirmOpen.set(false);
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
    const plan = this.currentPlan();
    if (!plan) return;
    this.abandoning.set(true);
    try {
      await this.service.abandonPlan(plan.id);
      this.abandonOpen.set(false);
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
      const created = await this.service.createDeployer(name);
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
}
