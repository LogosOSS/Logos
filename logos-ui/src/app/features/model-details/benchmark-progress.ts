import { benchmarkDatasetLabel, speculativeDecodingLabel } from './benchmark-configuration';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { batchingSignature, comparisonSettings } from './benchmark-isolation';
import { benchmarkResultRow } from './benchmark-result-row';

function completedAt(run: ModelProviderBenchmark): number {
  const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
  return ended ? Date.parse(ended) : Number.NaN;
}

/** Record history can span worker releases; adoption still requires a fresh comparison. */
function timelineSignature(run: ModelProviderBenchmark, includeNightlyVariants = false): string | null {
  const configuration = { ...run.configuration, nightly: {}, metadata: {} };
  if (includeNightlyVariants && run.configuration['serving']) {
    const serving = { ...run.configuration['serving'] as Record<string, unknown> };
    if (typeof serving['enable_prefix_caching'] === 'boolean') delete serving['enable_prefix_caching'];
    if (['Off', 'N-gram · 3 draft tokens'].includes(speculativeDecodingLabel(serving['speculative_config']))) {
      delete serving['speculative_config'];
    }
    return batchingSignature({ ...run, configuration: { ...configuration, serving } });
  }
  return batchingSignature({ ...run, configuration });
}

export function recordConfiguration(run: ModelProviderBenchmark): string {
  const settings = comparisonSettings(run);
  const values = [`Sequences ${settings['max_num_seqs'] ?? 'not recorded'}`, `batch tokens ${settings['max_num_batched_tokens'] ?? 'not recorded'}`];
  if (settings['enable_prefix_caching']) values.push(`prefix caching ${settings['enable_prefix_caching'].toLowerCase()}`);
  if (settings['speculative_config']) values.push(`speculative decoding ${settings['speculative_config']}`);
  return values.join(' · ');
}

/** Plot completed measurements in absolute units with each round's captured baseline. */
export function benchmarkProgress(runs: readonly ModelProviderBenchmark[]) {
  const byId = new Map(runs.map(run => [run.id, run]));
  const groups = new Map<string, {
    runs: Map<number, ModelProviderBenchmark>;
    baselines: Map<number, ModelProviderBenchmark>;
    measuredSettings: Set<string | null>;
  }>();
  for (const run of runs) {
    const nightly = run.configuration['nightly'] as { baseline_id?: number; reference_id?: number } | undefined;
    const baseline = byId.get(nightly?.baseline_id ?? nightly?.reference_id ?? -1);
    const signature = timelineSignature(run, true);
    if (!baseline || signature === null || signature !== timelineSignature(baseline, true)) continue;
    if (!Number.isFinite(completedAt(run)) || !Number.isFinite(completedAt(baseline))) continue;
    const before = comparisonValue(baseline, 'throughput'), after = comparisonValue(run, 'throughput');
    if (before === null || before <= 0 || after === null || after <= 0) continue;
    const group = groups.get(signature) ?? { runs: new Map(), baselines: new Map(), measuredSettings: new Set() };
    group.baselines.set(baseline.id, baseline);
    group.runs.set(baseline.id, baseline);
    group.runs.set(run.id, run);
    group.measuredSettings.add(timelineSignature(run));
    group.measuredSettings.add(timelineSignature(baseline));
    groups.set(signature, group);
  }
  for (const run of runs) {
    if (run.configuration['nightly']) continue;
    const key = timelineSignature(run, true), rate = comparisonValue(run, 'throughput');
    const group = key === null ? undefined : groups.get(key);
    if (group?.measuredSettings.has(timelineSignature(run)) && rate !== null && rate > 0 && Number.isFinite(completedAt(run))) {
      group.runs.set(run.id, run);
    }
  }
  return [...groups.entries()].map(([key, group]) => {
    const ordered = [...group.runs.values()].sort((a, b) => completedAt(a) - completedAt(b) || a.id - b.id);
    const baselines = [...group.baselines.values()].sort((a, b) => completedAt(a) - completedAt(b) || a.id - b.id);
    const reference = baselines[baselines.length - 1];
    const last = completedAt(ordered[ordered.length - 1]);
    const points = ordered.map(run => {
      const time = completedAt(run), rate = comparisonValue(run, 'throughput')!;
      const comparison = benchmarkResultRow(run, runs);
      return { run, time, rate,
        gain: comparison.delta, baselineId: comparison.baselineId,
        isBaseline: group.baselines.has(run.id), selectionNote: comparison.selectionNote,
      };
    });
    const maxRate = Math.max(...points.map(point => point.rate));
    const roughStep = maxRate / 5;
    const magnitude = 10 ** Math.floor(Math.log10(roughStep));
    const step = ([1, 2, 2.5, 5, 10].find(unit => unit * magnitude >= roughStep) ?? 10) * magnitude;
    const intervals = Math.ceil(maxRate / step);
    const ticks = Array.from({ length: intervals + 1 }, (_, i) => i * step);
    const settings = comparisonSettings(reference);
    const context = `${benchmarkDatasetLabel(reference)} · ${reference.provider_name} · ${reference.sample_size} requests · ${settings['max_concurrency']} concurrent`;
    const engine = reference.configuration['tool'] === 'logos-replay' ? 'Legacy replay' : 'GuideLLM';
    return { key, reference, points, ticks, axisMax: ticks[intervals], last, context,
      label: `${context} · ${engine}`,
      runIds: ordered.map(run => run.id),
    };
  }).sort((a, b) => b.last - a.last);
}
