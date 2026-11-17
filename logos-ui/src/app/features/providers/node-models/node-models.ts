import { ChangeDetectionStrategy, Component, computed, inject, input, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { DataTableComponent } from '../../../shared/components/data-table/data-table';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';

type StatusTone = 'ok' | 'gray' | 'warn' | 'err';

export interface CalibrationLabel {
  text: string;
  tone: StatusTone;
  title: string | null;
}

/** Settings of a lane or a benchmark run that decide where a model sits. */
const PLACEMENT_KEYS = ['tensor_parallel_size', 'kv_cache_memory_bytes', 'max_model_len'] as const;

/** The models of one logosnode: calibration and configuration state, one click to the model page. */
@Component({
  selector: 'app-node-models',
  standalone: true,
  imports: [DataTableComponent, ErrorMessageComponent, RouterLink],
  templateUrl: './node-models.html',
  styleUrl: './node-models.scss',
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeModels implements OnInit {
  private readonly service = inject(ProviderManagementService);

  readonly providerId = input.required<number>();

  readonly insights = signal<NodeInsights | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly models = computed(() => this.insights()?.models ?? []);
  readonly workerOnline = computed(() => this.insights()?.worker.online ?? false);

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.insights.set(await this.service.getNodeInsights(this.providerId()));
    } catch (e) {
      this.error.set(this.messageOf(e));
    } finally {
      this.loading.set(false);
    }
  }

  /** Calibration column: status, with "tuning available" only where a benchmark found a better setting. */
  calibrationLabel(model: NodeModelInsight): CalibrationLabel {
    const { status, reason } = model.calibration;
    if (status === 'unsupported') {
      return { text: 'Unsupported', tone: 'err', title: reason };
    }
    if (status === 'calibrated') {
      return this.hasTuning(model)
        ? { text: 'Calibrated · tuning available', tone: 'ok', title: null }
        : { text: 'Calibrated', tone: 'ok', title: null };
    }
    if (model.calibrating) {
      return { text: 'Calibrating…', tone: 'warn', title: null };
    }
    if (status === 'outdated') {
      return { text: 'Calibration outdated', tone: 'gray', title: null };
    }
    return { text: 'Not calibrated', tone: 'gray', title: null };
  }

  /** A benchmark run that is comparable and placed the model differently from how it runs now. */
  hasTuning(model: NodeModelInsight): boolean {
    const best = model.benchmark?.best_eligible;
    if (!best || !model.benchmark?.eligible_for_comparison) {
      return false;
    }
    const current = model.benchmark.current;
    return PLACEMENT_KEYS.some((key) => key in best.settings && String(best.settings[key]) !== String(current[key]));
  }

  private messageOf(e: unknown): string {
    const body = (e as { error?: { error?: string }; message?: string } | null) ?? null;
    return body?.error?.error || body?.message || 'The models of this node could not be loaded.';
  }
}
