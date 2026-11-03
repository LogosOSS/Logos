import { benchmarkDatasetLabel, isSavedRequestBenchmark } from './benchmark-configuration';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { batchingSignature, comparisonSettings } from './benchmark-isolation';

function completedAt(run: ModelProviderBenchmark): number {
  const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
  return ended ? Date.parse(ended) : NaN;
}

/** Record history can span worker releases; adoption still requires a fresh comparison. */
function timelineSignature(run: ModelProviderBenchmark): string | null {
  return batchingSignature({ ...run, configuration: { ...run.configuration,
    nightly: {}, metadata: {} } });
}

export function recordConfiguration(run: ModelProviderBenchmark): string {
  const settings = comparisonSettings(run);
  return `Sequences ${settings['max_num_seqs'] ?? 'auto'} · batch tokens ${settings['max_num_batched_tokens'] ?? 'auto'}`;
}

/** Start at the first baseline and plot only strict throughput records at their actual measurement times. */
export function benchmarkProgress(runs: readonly ModelProviderBenchmark[]) {
  const byId = new Map(runs.map(run => [run.id, run]));
  const groups = new Map<string, { reference: ModelProviderBenchmark; runs: Map<number, ModelProviderBenchmark> }>();
  for (const run of runs) {
    const nightly = run.configuration['nightly'] as { baseline_id?: number; reference_id?: number; eligible_for_selection?: boolean } | undefined;
    const baseline = byId.get(nightly?.baseline_id ?? nightly?.reference_id ?? -1);
    const signature = timelineSignature(run);
    if (!baseline || signature === null || signature !== timelineSignature(baseline)) continue;
    if (!Number.isFinite(completedAt(run)) || !Number.isFinite(completedAt(baseline))) continue;
    const before = comparisonValue(baseline, 'throughput'), after = comparisonValue(run, 'throughput');
    if (before === null || before <= 0 || after === null || after <= 0) continue;
    const group = groups.get(signature) ?? { reference: baseline, runs: new Map<number, ModelProviderBenchmark>() };
    if (completedAt(baseline) < completedAt(group.reference)
      || (completedAt(baseline) === completedAt(group.reference) && baseline.id < group.reference.id)) group.reference = baseline;
    group.runs.set(baseline.id, baseline);
    // A saved-request variant must pass the response and latency checks before it can set a record.
    if (!isSavedRequestBenchmark(run) || run.id === baseline.id || nightly?.eligible_for_selection === true) group.runs.set(run.id, run);
    groups.set(signature, group);
  }
  for (const run of runs) {
    if (run.configuration['nightly']) continue;
    const key = timelineSignature(run), rate = comparisonValue(run, 'throughput');
    if (key !== null && rate !== null && rate > 0 && Number.isFinite(completedAt(run))) groups.get(key)?.runs.set(run.id, run);
  }
  return [...groups.entries()].map(([key, group]) => {
    const reference = group.reference, first = completedAt(reference);
    const referenceRate = comparisonValue(reference, 'throughput')!;
    let bestRate = referenceRate, previousRunId = reference.id;
    const points = [{ run: reference, time: first, rate: referenceRate, gain: 0,
      stepGain: null as number | null, previousRunId: null as number | null, key: `${reference.id}` }];
    const ordered = [...group.runs.values()].sort((a, b) => completedAt(a) - completedAt(b) || a.id - b.id);
    for (const run of ordered) {
      const time = completedAt(run), rate = comparisonValue(run, 'throughput')!;
      if (time < first || (time === first && run.id <= reference.id) || rate <= bestRate) continue;
      points.push({ run, time, rate, gain: (rate / referenceRate - 1) * 100,
        stepGain: (rate / bestRate - 1) * 100, previousRunId, key: `${run.id}` });
      bestRate = rate; previousRunId = run.id;
    }
    const last = Math.max(first, ...ordered.map(completedAt));
    const padding = Math.max(referenceRate * .05, (bestRate - referenceRate) * .15, 1);
    const min = Math.max(0, referenceRate - padding), max = bestRate + padding;
    const y = (rate: number) => (max - rate) / (max - min) * 100;
    const plotted = points.map(point => ({ ...point, configuration: recordConfiguration(point.run),
      x: first === last ? 50 : (point.time - first) / (last - first) * 100, y: y(point.rate) }));
    const latest = plotted[plotted.length - 1];
    const settings = comparisonSettings(reference);
    return { key, fixed: isSavedRequestBenchmark(reference), reference, latest, first, last, min, max,
      zero: y(referenceRate), points: plotted,
      line: plotted.map(point => `${point.x},${point.y}`).join(' '),
      label: `${benchmarkDatasetLabel(reference)} · ${reference.provider_name}`,
      context: `${benchmarkDatasetLabel(reference)} · ${reference.provider_name} · ${reference.sample_size} requests · ${settings['max_concurrency']} concurrent`,
    };
  }).sort((a, b) => b.last - a.last);
}
