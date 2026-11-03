import { ChangeDetectionStrategy, Component, computed, effect, inject, input, OnDestroy, signal } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { NightlyStatus } from './nightly-benchmarks';
import { benchmarkDatasetLabel, servingConfigurationItems } from './benchmark-configuration';
import { benchmarkResultRow } from './benchmark-result-row';

interface SelectionComparison { benchmark_id: number; baseline_id: number | null; throughput: number | null; issues: string[]; }
interface DeploymentStatus extends NightlyStatus { deployment?: DeploymentSnapshot; selection?: { best_observed: SelectionComparison | null; best_eligible: SelectionComparison | null; comparisons: SelectionComparison[] }; }

export interface DeploymentSnapshot {
  available: boolean;
  observed_at?: string;
  lane_state?: string;
  maintenance: boolean;
  series_key?: string | null;
  configuration: Record<string, unknown>;
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value.map(canonical));
  if (value !== null && typeof value === 'object') return JSON.stringify(Object.entries(value)
    .filter(([key, item]) => key !== 'command' && item != null).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => [key, canonical(item)]));
  return JSON.stringify(value);
}

/** A recorded winner alone does not establish what the worker serves now. */
export function deployedMeasurement(deployment: DeploymentSnapshot | undefined, runs: readonly ModelProviderBenchmark[], sameEnvironment = true) {
  if (!deployment?.available || deployment.maintenance || (sameEnvironment && !deployment.series_key) || !Object.keys(deployment.configuration).length) return null;
  const current = canonical(deployment.configuration);
  return [...runs].sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id).find(run => {
    const nightly = run.configuration['nightly'] as { series_key?: string } | undefined;
    const totals = run.metrics.request_totals;
    return (!sameEnvironment || nightly?.series_key === deployment.series_key) && canonical(run.configuration['serving']) === current
      && run.sample_size > 0 && totals?.successful === run.sample_size && totals.total === run.sample_size
      && totals.errored === 0 && totals.incomplete === 0;
  }) ?? null;
}

@Component({
  selector: 'app-deployed-configuration', standalone: true, imports: [DatePipe, DecimalPipe, RouterLink],
  templateUrl: './deployed-configuration.html', styleUrl: './deployed-configuration.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DeployedConfiguration implements OnDestroy {
  readonly pairId = input.required<number>();
  readonly providerName = input.required<string>();
  readonly runs = input.required<readonly ModelProviderBenchmark[]>();
  private readonly http = inject(HttpClient);
  readonly state = signal<DeploymentStatus | null>(null);
  readonly error = signal(false);
  readonly datasetLabel = benchmarkDatasetLabel;
  readonly deployment = computed(() => this.state()?.deployment);
  readonly measurement = computed(() => deployedMeasurement(this.deployment(), this.runs().filter(run => run.model_provider_id === this.pairId())));
  readonly historicalMeasurement = computed(() => this.measurement() ?? deployedMeasurement(this.deployment(),
    this.runs().filter(run => run.model_provider_id === this.pairId()), false));
  readonly result = computed(() => this.historicalMeasurement() ? benchmarkResultRow(this.historicalMeasurement()!, this.runs()) : null);
  readonly parameters = computed(() => servingConfigurationItems({ configuration: { serving: this.deployment()?.configuration } }).filter(item => item.captured));
  readonly keyParameters = computed(() => this.parameters().filter(item => ['max_num_seqs', 'max_num_batched_tokens'].includes(item.key)));
  readonly selection = computed(() => this.state()?.selection);
  readonly adoption = computed(() => this.state()?.optimizer?.production_adoption);
  private timer?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private destroyed = false;

  constructor() {
    effect(() => { const id = this.pairId(); this.state.set(null); this.error.set(false); void this.load(id, ++this.generation); });
  }
  ngOnDestroy(): void { this.destroyed = true; this.generation++; clearTimeout(this.timer); }
  private async load(id: number, generation: number): Promise<void> {
    clearTimeout(this.timer);
    try {
      const state = await firstValueFrom(this.http.post<DeploymentStatus>(
        '/api/logosdb/model_benchmarks/nightly/status', { model_provider_id: id }));
      if (this.destroyed || generation !== this.generation) return;
      this.state.set(state); this.error.set(false);
    } catch {
      if (this.destroyed || generation !== this.generation) return;
      this.state.set(null); this.error.set(true);
    } finally {
      if (!this.destroyed && generation === this.generation) this.timer = setTimeout(() => void this.load(id, generation), 15000);
    }
  }
}
