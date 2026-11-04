import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';

/** Use the captured same-round baseline, including its zero-change measurement. */
export function benchmarkResultRow(run: ModelProviderBenchmark, runs: readonly ModelProviderBenchmark[]) {
  const nightly = run.configuration['nightly'] as {
    baseline_id?: number; eligible_for_selection?: boolean; selection_issues?: string[]; parameters?: Record<string, unknown>; original?: Record<string, unknown>;
  } | undefined;
  const baseline = runs.find(candidate => candidate.id === nightly?.baseline_id
    && candidate.model_provider_id === run.model_provider_id);
  const isBaseline = baseline?.id === run.id;
  const before = baseline ? comparisonValue(baseline, 'throughput') : null;
  const after = comparisonValue(run, 'throughput');
  const measurement = run.configuration['measurement'] as { started_at?: string; ended_at?: string } | undefined;
  const measured = measurement?.started_at ? Date.parse(measurement.started_at) : Number.NaN;
  const requested = run.configuration['requested_overrides'] as Record<string, unknown> | undefined;
  const label = (key: string) => key === 'max_num_seqs' ? 'Sequences' : key === 'max_num_batched_tokens' ? 'Batch tokens' : key;
  const changes = nightly ? Object.entries(nightly.parameters ?? {}).filter(([key, value]) => value !== nightly.original?.[key])
    .map(([key, value]) => `${label(key)}: ${nightly.original?.[key] ?? 'auto'} → ${value}`)
    : Object.entries(requested ?? {}).map(([key, value]) => `${label(key)} → ${typeof value === 'object' ? JSON.stringify(value) : value}`);
  return { run, runId: run.id, measuredFrom: Number.isFinite(measured) ? measured : null,
    before, after, isBaseline, measurement, changes,
    selectionNote: nightly?.eligible_for_selection === false ? 'Not eligible for automatic adoption: ' + (nightly.selection_issues?.join(' · ') || 'workload, output completeness, token counts or p95 latency failed the comparison checks.') : null, baselineId: nightly?.baseline_id,
    comparisonNote: isBaseline ? 'Starting measurement · compared with itself'
      : baseline ? `Compared with baseline #${baseline.id}`
      : nightly?.baseline_id ? `Baseline #${nightly.baseline_id} is no longer available` : 'No comparison baseline was recorded',
    currentSettings: !nightly && requested !== undefined && Object.keys(requested).length === 0,
    kind: nightly ? (isBaseline ? 'Nightly baseline' : 'Nightly variant') : 'Benchmark',
    delta: before !== null && before > 0 && after !== null ? (after / before - 1) * 100 : null };
}
