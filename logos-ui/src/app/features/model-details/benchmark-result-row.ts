import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { speculativeDecodingLabel } from './benchmark-configuration';

export interface NightlyParameterChange {
  parameter: string;
  before: unknown;
  after: unknown;
}

const labels = new Map([
  ['max_num_seqs', 'Sequences'],
  ['max_num_batched_tokens', 'Batch tokens'],
  ['kv_cache_dtype', 'KV cache dtype'],
  ['enable_prefix_caching', 'Prefix caching'],
  ['speculative_config', 'Speculative decoding'],
]);

function parameterValue(parameter: string, value: unknown): string {
  if (parameter === 'speculative_config') return speculativeDecodingLabel(value ?? '');
  if (parameter === 'enable_prefix_caching') return value === true ? 'Enabled' : value === false ? 'Disabled' : 'Not reported';
  if (parameter === 'kv_cache_dtype' && value === 'fp8_e4m3') return 'fp8';
  if (parameter === 'max_num_batched_tokens' && value == null) return 'Not reported';
  if (value == null || value === '' || value === 'auto' || (parameter === 'max_num_seqs' && value === 0)) return 'Auto';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function displayParameterChange({ parameter, before, after }: NightlyParameterChange) {
  return { parameter, label: labels.get(parameter) ?? parameter,
    before: parameterValue(parameter, before), after: parameterValue(parameter, after) };
}

export interface SavedNightlyComparison {
  baseline_id?: number;
  eligible_for_selection?: boolean;
  selection_issues?: string[];
  parameters?: Record<string, unknown>;
  original?: Record<string, unknown>;
}

export function recordedNightlyChanges(run: ModelProviderBenchmark) {
  const nightly = run.configuration['nightly'] as SavedNightlyComparison | undefined;
  return Object.entries(nightly?.parameters ?? {}).map(([parameter, after]) =>
    displayParameterChange({ parameter, before: nightly?.original?.[parameter], after }))
    .filter(change => change.before !== change.after);
}

/** Use the captured same-round baseline, including its zero-change measurement. */
export function benchmarkResultRow(run: ModelProviderBenchmark, runs: readonly ModelProviderBenchmark[]) {
  const nightly = run.configuration['nightly'] as SavedNightlyComparison | undefined;
  const baseline = runs.find(candidate => candidate.id === nightly?.baseline_id
    && candidate.model_provider_id === run.model_provider_id);
  const isBaseline = baseline?.id === run.id;
  const before = baseline ? comparisonValue(baseline, 'throughput') : null;
  const after = comparisonValue(run, 'throughput');
  const measurement = run.configuration['measurement'] as { started_at?: string; ended_at?: string } | undefined;
  const measured = measurement?.started_at ? Date.parse(measurement.started_at) : Number.NaN;
  const requested = run.configuration['requested_overrides'] as Record<string, unknown> | undefined;
  const parameterChanges = nightly ? recordedNightlyChanges(run) : [];
  const changes = nightly ? parameterChanges.map(change => `${change.label}: ${change.before} → ${change.after}`)
    : Object.entries(requested ?? {}).map(([key, value]) => `${labels.get(key) ?? key} → ${
      key === 'enable_prefix_caching' || key === 'speculative_config' ? parameterValue(key, value)
        : typeof value === 'object' ? JSON.stringify(value) : value}`);
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
