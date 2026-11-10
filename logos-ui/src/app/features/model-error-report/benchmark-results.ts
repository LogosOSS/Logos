import { afterRenderEffect, ChangeDetectionStrategy, Component, computed, effect, ElementRef, input, signal, viewChild } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { benchmarkConfigurationItems, benchmarkDatasetLabel, servingConfigurationItems } from './benchmark-configuration';
import { comparisonValue } from './benchmark-comparison';
import { benchmarkProgress } from './benchmark-progress';
import { batchingSignature } from './benchmark-isolation';
import { benchmarkResultRow } from './benchmark-result-row';

type ResultColumn = 'runId' | 'measuredFrom' | 'before' | 'after' | 'delta';

@Component({
  selector: 'app-benchmark-results',
  standalone: true,
  imports: [DatePipe, DecimalPipe, RouterLink],
  templateUrl: './benchmark-results.html',
  styleUrl: './benchmark-results.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class BenchmarkResults {
  throughput(run: ModelProviderBenchmark) { return comparisonValue(run, 'throughput'); }
  readonly datasetLabel = benchmarkDatasetLabel;
  readonly runs = input.required<readonly ModelProviderBenchmark[]>();
  readonly selectedRunId = input<number | null>(null);
  readonly showProgress = input(true);
  readonly showHistory = input(true);
  readonly selectedRow = computed(() => this.rows().find(row => row.run.id === this.selectedRunId()) ?? null);
  private readonly selectedResult = viewChild<ElementRef<HTMLElement>>('selectedResult');
  private focusedRunId: number | null = null;
  readonly progress = computed(() => benchmarkProgress(this.runs()));
  readonly progressKey = signal('');
  readonly activeProgress = computed(() => {
    const reference = (this.selectedRow()?.run.configuration['nightly'] as { reference_id?: number } | undefined)?.reference_id;
    return this.progress().find(group => group.key === this.progressKey())
      ?? this.progress().find(group => group.reference.id === reference) ?? this.progress()[0];
  });
  readonly showAll = signal(false);
  readonly sort = signal<{ key: ResultColumn; direction: 'ascending' | 'descending' } | null>(null);
  readonly columns: readonly { key: ResultColumn; label: string }[] = [
    { key: 'runId', label: 'Run' }, { key: 'measuredFrom', label: 'Measured from → to' },
    { key: 'before', label: 'Before' }, { key: 'after', label: 'After' }, { key: 'delta', label: 'Change' },
  ];
  readonly sortedRows = computed(() => {
    const sort = this.sort();
    if (!sort) return this.rows();
    return [...this.rows()].sort((a, b) => {
      const left = a[sort.key], right = b[sort.key];
      if (left === null) return right === null ? 0 : 1;
      if (right === null) return -1;
      return sort.direction === 'ascending' ? left - right : right - left;
    });
  });
  readonly datasetGroups = computed(() => {
    const groups = new Map<string, { key: string; label: string; rows: ReturnType<BenchmarkResults['rows']> }>();
    // Dataset order stays newest first; sorting changes the runs within each dataset.
    for (const row of this.rows()) {
      const workload = row.run.configuration['workload'] as { id?: string } | undefined;
      const key = workload?.id ? `requests:${workload.id}` : row.run.dataset;
      if (!groups.has(key)) groups.set(key, { key, label: benchmarkDatasetLabel(row.run), rows: [] });
    }
    for (const row of this.sortedRows()) {
      const workload = row.run.configuration['workload'] as { id?: string } | undefined;
      groups.get(workload?.id ? `requests:${workload.id}` : row.run.dataset)!.rows.push(row);
    }
    return [...groups.values()].map(group => ({ ...group,
      visible: this.showAll() ? group.rows : group.rows.filter((row, index) => index < 5 || row.run.id === this.selectedRunId()) }));
  });
  readonly visibleRows = computed(() => this.datasetGroups().flatMap(group => group.visible));
  readonly hasMore = computed(() => this.datasetGroups().some(group => group.rows.length > 5));

  constructor() {
    effect(() => {
      if (this.selectedRunId() !== null) this.progressKey.set('');
    });
    afterRenderEffect(() => {
      const element = this.selectedResult()?.nativeElement;
      const id = this.selectedRunId();
      if (id === null) this.focusedRunId = null;
      if (element && id !== this.focusedRunId) {
        element.scrollIntoView?.({ block: 'center' });
        element.focus({ preventScroll: true });
        this.focusedRunId = id;
      }
    });
  }

  sortBy(key: ResultColumn): void {
    this.sort.update(previous => previous?.key === key
      ? previous.direction === 'ascending' ? { key, direction: 'descending' } : null
      : { key, direction: 'ascending' });
  }

  readonly topRuns = computed(() => {
    const best = new Map<number, ModelProviderBenchmark>();
    const workloads = new Map<number, string>();
    for (const run of [...this.runs()].sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id)) {
      const totals = run.metrics.request_totals;
      const rate = comparisonValue(run, 'throughput');
      if (rate === null || run.sample_size <= 0 || totals?.successful !== run.sample_size
        || totals.total !== run.sample_size || totals.errored !== 0 || totals.incomplete !== 0) continue;
      const workload = batchingSignature(run) ?? `uncaptured:${run.id}`;
      if (!workloads.has(run.model_provider_id)) workloads.set(run.model_provider_id, workload);
      if (workloads.get(run.model_provider_id) !== workload) continue;
      const previous = best.get(run.model_provider_id);
      if (!previous || rate > comparisonValue(previous, 'throughput')!
        || (rate === comparisonValue(previous, 'throughput') && run.id > previous.id)) {
        best.set(run.model_provider_id, run);
      }
    }
    return [...best.values()].sort((a, b) => a.provider_name.localeCompare(b.provider_name) || a.model_provider_id - b.model_provider_id).map(run => ({
      run, rate: comparisonValue(run, 'throughput'),
      workload: benchmarkConfigurationItems(run), parameters: servingConfigurationItems(run),
    }));
  });
  readonly rows = computed(() => [...this.runs()].sort((a, b) => Date.parse(b.recorded_at) - Date.parse(a.recorded_at) || b.id - a.id)
    .map(run => benchmarkResultRow(run, this.runs())));
}
