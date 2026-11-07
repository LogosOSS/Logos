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
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { DeploymentService } from '../../core/services/deployment.service';
import {
  Deployer,
  DeployerCreated,
  DeployerKind,
  DeploymentOverview,
  DeploymentPlan,
  DeploymentTarget,
  PLAN_STATUS_LABEL,
  PlanStatus,
  STEP_STATUS_LABEL,
  StatusTone,
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
import { SelectComponent, AppSelectOption } from '../../shared/components/select/select';
import { PhaseBar } from './phase-bar';
import { StatusPill } from './status-pill';

/** How often the page reads the state again. A deployment takes minutes, so a few seconds is soon enough. */
const POLL_MS = 5000;

/** The order of the environments in the switch. Others follow, by name. */
const ENVIRONMENT_ORDER = ['Prod', 'Dev', 'Test'];

@Component({
  selector: 'app-deployments',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    RouterLink,
    SelectComponent,
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
  private route = inject(ActivatedRoute);
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
  /** The steps of the running plan: the list has only one dot for each step. */
  runningDetail = signal<DeploymentPlan | null>(null);
  selectedEnv = signal('');
  statusFilter = signal<'all' | PlanStatus>('all');
  loading = signal(true);
  loadError = signal<string | null>(null);

  // ── starting a deployment ────────────────────────────────────────────────
  tag = signal('latest');
  /** The typed name of a protected environment. */
  confirmation = signal('');
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
  targets = computed(() => {
    const rank = (name: string) => {
      const index = ENVIRONMENT_ORDER.indexOf(name);
      return index === -1 ? ENVIRONMENT_ORDER.length : index;
    };
    return [...(this.overview()?.targets ?? [])].sort(
      (a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name),
    );
  });
  target = computed(() => this.targets().find((t) => t.name === this.selectedEnv()) ?? null);
  currentPlan = computed(() => this.plans().find((p) => p.status === 'running') ?? null);
  lastPlan = computed(() => this.plans()[0] ?? null);
  history = computed(() => {
    const filter = this.statusFilter();
    return this.plans().filter((p) => filter === 'all' || p.status === filter);
  });
  /** The typed word that a protected environment asks for: its name, in lower case. */
  confirmWord = computed(() => this.selectedEnv().toLowerCase());
  confirmed = computed(
    () => !this.target()?.requires_confirmation || this.confirmation().trim().toLowerCase() === this.confirmWord(),
  );
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
        const names = this.targets().map((t) => t.name);
        if (!names.includes(this.selectedEnv())) {
          // The environment in the address wins, so that a reload or the way back from a deployment keeps it.
          const requested = this.route.snapshot.queryParamMap.get('env');
          this.selectedEnv.set(
            requested && names.includes(requested)
              ? requested
              : overview.environment && names.includes(overview.environment)
                ? overview.environment
                : (names[0] ?? ''),
          );
        }
        await this.loadPlans();
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
    const running = plans.find((p) => p.status === 'running');
    const detail = running ? await this.service.getPlan(running.id).catch(() => null) : null;
    if (env === this.selectedEnv()) this.runningDetail.set(detail);
  }

  async selectEnv(name: string): Promise<void> {
    if (name === this.selectedEnv()) return;
    this.selectedEnv.set(name);
    void this.router.navigate([], { queryParams: { env: name }, queryParamsHandling: 'merge', replaceUrl: true });
    this.plans.set([]);
    this.runningDetail.set(null);
    this.startError.set(null);
    this.confirmation.set('');
    try {
      await this.loadPlans();
    } catch (e) {
      this.loadError.set(errorDetail(e) ?? 'The deployments could not be loaded.');
    }
  }

  // ── starting ─────────────────────────────────────────────────────────────
  async deploy(): Promise<void> {
    this.startError.set(null);
    const target = this.target();
    if (!target) return;
    if (!this.tag().trim()) {
      this.startError.set('Enter an image tag.');
      return;
    }
    if (!this.confirmed()) {
      this.startError.set(`Type ${this.confirmWord()} to confirm.`);
      return;
    }
    this.starting.set(true);
    try {
      const plan = await this.service.startPlan(
        target.name,
        this.tag().trim(),
        target.requires_confirmation ? this.confirmation().trim() : undefined,
      );
      this.confirmation.set('');
      await this.router.navigate(['/deployments', plan.id]);
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
  /** The dot of an environment in the switch: how its latest deployment ended, or an empty ring if there is none. */
  envDot(target: DeploymentTarget): StatusTone | 'none' {
    return target.last_status ? planTone(target.last_status) : 'none';
  }
}
