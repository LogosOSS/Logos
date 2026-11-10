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
import { ActivatedRoute, RouterLink } from '@angular/router';
import { DeploymentService } from '../../core/services/deployment.service';
import {
  DeploymentPlan,
  DeploymentStep,
  PLAN_STATUS_LABEL,
  clockTime,
  eventTitle,
  fullDate,
  nodeKindLabel,
  phaseStates,
  planTone,
  stepStatusText,
  stepTone,
} from '../../shared/models/deployment.model';
import { errorDetail } from '../../shared/utils/error-detail';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { PhaseBar } from './phase-bar';
import { StatusPill } from './status-pill';

/** A deployment takes minutes: the page reads it again every few seconds while it runs. */
const POLL_MS = 5000;

@Component({
  selector: 'app-deployment-detail',
  standalone: true,
  imports: [CommonModule, RouterLink, ModalConfirmComponent, PhaseBar, StatusPill],
  templateUrl: './deployment-detail.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ['./deployments-shared.scss', './deployment-detail.scss'],
})
export class DeploymentDetail implements OnInit {
  private service = inject(DeploymentService);
  private route = inject(ActivatedRoute);
  private destroyRef = inject(DestroyRef);

  readonly planLabel = PLAN_STATUS_LABEL;
  readonly planTone = planTone;
  readonly stepTone = stepTone;
  readonly phaseStates = phaseStates;
  readonly stepStatusText = stepStatusText;
  readonly nodeKindLabel = nodeKindLabel;
  readonly clockTime = clockTime;
  readonly eventTitle = eventTitle;
  readonly fullDate = fullDate;

  plan = signal<DeploymentPlan | null>(null);
  loading = signal(true);
  loadError = signal<string | null>(null);
  copied = signal(false);
  /** The clock of the running time: it ticks every second while the deployment runs. */
  private now = signal(Date.now());
  /** The first load failed for a reason that passes (Logos restarts): the timer asks again. */
  private retryFirstLoad = false;

  abandonOpen = signal(false);
  abandoning = signal(false);
  abandonError = signal<string | null>(null);

  steps = computed(() => this.plan()?.step_details ?? []);
  /** The container log of the first node that failed. Nodes that did not fail have none. */
  logStep = computed<DeploymentStep | null>(() => this.steps().find((s) => !!s.failure_logs) ?? null);
  canAbandon = computed(() => this.plan()?.status === 'running' && this.plan()?.origin === 'local');
  /** "6m 12s", or "3h 4m" for a long one. A running deployment counts up. */
  duration = computed(() => {
    const plan = this.plan();
    if (!plan) return '';
    const end = plan.finished_at ? new Date(plan.finished_at).getTime() : this.now();
    const seconds = Math.max(0, Math.round((end - new Date(plan.created_at).getTime()) / 1000));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
  });

  ngOnInit(): void {
    void this.load();
    const timer = setInterval(() => {
      if (this.plan()?.status === 'running' || (this.plan() === null && this.retryFirstLoad)) void this.load();
    }, POLL_MS);
    const clock = setInterval(() => {
      if (this.plan()?.status === 'running') this.now.set(Date.now());
    }, 1000);
    this.destroyRef.onDestroy(() => {
      clearInterval(timer);
      clearInterval(clock);
    });
  }

  async load(): Promise<void> {
    const id = this.route.snapshot.paramMap.get('id');
    if (!id) return;
    try {
      this.plan.set(await this.service.getPlan(id));
      this.loadError.set(null);
    } catch (e) {
      if (this.plan() === null) {
        this.loadError.set(errorDetail(e) ?? 'The deployment could not be loaded.');
        // No answer (status 0) or a server error: Logos may be restarting. A 403 or 404 does not pass.
        const status = (e as { status?: number } | null)?.status;
        this.retryFirstLoad = status === undefined || status === 0 || status >= 500;
      }
    } finally {
      this.loading.set(false);
    }
  }

  askAbandon(): void {
    this.abandonError.set(null);
    this.abandonOpen.set(true);
  }

  closeAbandon(): void {
    this.abandonOpen.set(false);
  }

  async confirmAbandon(): Promise<void> {
    const plan = this.plan();
    if (!plan) return;
    this.abandoning.set(true);
    try {
      await this.service.abandonPlan(plan.id);
      this.abandonOpen.set(false);
      await this.load();
    } catch (e) {
      this.abandonError.set(errorDetail(e) ?? 'The deployment could not be abandoned.');
    } finally {
      this.abandoning.set(false);
    }
  }

  async copyLog(): Promise<void> {
    const text = this.logStep()?.failure_logs;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      this.copied.set(true);
    } catch {
      this.copied.set(false);
    }
  }
}
