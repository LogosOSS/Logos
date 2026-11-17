import { ChangeDetectionStrategy, Component, computed, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { combineLatest } from 'rxjs';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { NodeConfig, NodeInsights } from '../../../shared/models/node-config.model';
import { CalibrationLabel, calibrationLabel, hasTuning } from '../node-models/node-model-status';
import {
  advancedSettings,
  describePlacement,
  entrySettings,
  formatGb,
  formatSeconds,
  formatTokens,
  kvToGb,
  mbToGb,
} from './node-model-format';

/** One model on one logosnode: its configuration and what Logos measured. */
@Component({
  selector: 'app-node-model',
  standalone: true,
  imports: [ErrorMessageComponent, RouterLink],
  templateUrl: './node-model.html',
  styleUrl: './node-model.scss',
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeModel implements OnInit {
  private readonly service = inject(ProviderManagementService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly providerId = signal<number | null>(null);
  readonly modelName = signal('');
  readonly providerName = signal('');
  readonly config = signal<NodeConfig | null>(null);
  readonly insights = signal<NodeInsights | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly insight = computed(() => this.insights()?.models.find((m) => m.model_name === this.modelName()) ?? null);
  readonly entry = computed(() => this.config()?.entries.find((e) => e.model_name === this.modelName()));
  readonly workerOnline = computed(() => this.insights()?.worker.online ?? false);
  readonly settings = computed(() => entrySettings(this.entry()));
  readonly advanced = computed(() => advancedSettings(this.entry()));
  readonly label = computed<CalibrationLabel | null>(() => {
    const insight = this.insight();
    return insight ? calibrationLabel(insight) : null;
  });
  readonly tuning = computed(() => {
    const insight = this.insight();
    return insight !== null && hasTuning(insight);
  });
  readonly recommendation = computed(() => this.insight()?.benchmark?.best_eligible ?? null);
  /** The measured result of the calibrated tensor-parallel size. */
  readonly result = computed(() => this.insight()?.calibration.results.find((r) => r.active) ?? null);

  readonly kvGb = computed(() => kvToGb(this.settings()['kv_cache_memory_bytes']));

  readonly formatGb = formatGb;
  readonly formatSeconds = formatSeconds;
  readonly formatTokens = formatTokens;
  readonly describePlacement = describePlacement;
  readonly mbToGb = mbToGb;

  ngOnInit(): void {
    combineLatest([this.route.paramMap, this.route.queryParamMap])
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(([params, query]) => {
        this.providerId.set(Number(params.get('providerId')));
        this.modelName.set(query.get('model') ?? '');
        void this.load();
      });
  }

  async load(): Promise<void> {
    const id = this.providerId();
    if (id === null || Number.isNaN(id) || !this.modelName()) {
      this.loading.set(false);
      this.error.set('This page needs a node and a model.');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    try {
      const [config, insights, providers] = await Promise.all([
        this.service.getNodeConfig(id),
        this.service.getNodeInsights(id),
        this.service.getProviders(),
      ]);
      this.config.set(config);
      this.insights.set(insights);
      this.providerName.set(providers.find((p) => p.id === id)?.name ?? `Node ${id}`);
    } catch (e) {
      this.error.set(this.messageOf(e));
    } finally {
      this.loading.set(false);
    }
  }

  private messageOf(e: unknown): string {
    const body = (e as { error?: { error?: string }; message?: string } | null) ?? null;
    return body?.error?.error || body?.message || 'This model could not be loaded.';
  }
}
