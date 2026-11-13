import { benchmarkDatasetLabel, isSavedRequestBenchmark } from './benchmark-configuration';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { batchingSignature, comparisonSettings } from './benchmark-isolation';
import { benchmarkResultRow } from './benchmark-result-row';

function completedAt(run: ModelProviderBenchmark): number {
  const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
  return ended ? Date.parse(ended) : Number.NaN;
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

/** Plot completed measurements in absolute units with each round's captured baseline. */
export function benchmarkProgress(runs: readonly ModelProviderBenchmark[]) {
  const byId = new Map(runs.map(run => [run.id, run]));
  const groups = new Map<string, {
    runs: Map<number, ModelProviderBenchmark>;
    baselines: Map<number, ModelProviderBenchmark>;
  }>();
  for (const run of runs) {
    const nightly = run.configuration['nightly'] as { baseline_id?: number; reference_id?: number } | undefined;
    const baseline = byId.get(nightly?.baseline_id ?? nightly?.reference_id ?? -1);
    const signature = timelineSignature(run);
    if (!baseline || signature === null || signature !== timelineSignature(baseline)) continue;
    if (!Number.isFinite(completedAt(run)) || !Number.isFinite(completedAt(baseline))) continue;
    const before = comparisonValue(baseline, 'throughput'), after = comparisonValue(run, 'throughput');
    if (before === null || before <= 0 || after === null || after <= 0) continue;
    const group = groups.get(signature) ?? { runs: new Map(), baselines: new Map() };
    group.baselines.set(baseline.id, baseline);
    group.runs.set(baseline.id, baseline);
    group.runs.set(run.id, run);
    groups.set(signature, group);
  }
  for (const run of runs) {
    if (run.configuration['nightly']) continue;
    const key = timelineSignature(run), rate = comparisonValue(run, 'throughput');
    if (key !== null && rate !== null && rate > 0 && Number.isFinite(completedAt(run))) groups.get(key)?.runs.set(run.id, run);
  }
  return [...groups.entries()].map(([key, group]) => {
    const ordered = [...group.runs.values()].sort((a, b) => completedAt(a) - completedAt(b) || a.id - b.id);
    const baselines = [...group.baselines.values()].sort((a, b) => completedAt(a) - completedAt(b) || a.id - b.id);
    const reference = baselines[baselines.length - 1];
    const first = completedAt(ordered[0]), last = completedAt(ordered[ordered.length - 1]);
    const maxRate = Math.max(1, Math.ceil(Math.max(...ordered.map(run => comparisonValue(run, 'throughput')!)) * 1.1));
    const points = ordered.map(run => {
      const time = completedAt(run), rate = comparisonValue(run, 'throughput')!;
      const comparison = benchmarkResultRow(run, runs);
      return { run, time, rate, key: String(run.id), configuration: recordConfiguration(run),
        gain: comparison.delta, baselineId: comparison.baselineId, baselineRate: comparison.before,
        isBaseline: group.baselines.has(run.id), selectionNote: comparison.selectionNote,
        x: first === last ? 50 : (time - first) / (last - first) * 100,
        y: (1 - rate / maxRate) * 100 };
    });
    const baselinePoints = points.filter(point => point.isBaseline);
    const baselineLine = baselinePoints.flatMap((point, index) => index === 0
      ? [`${point.x},${point.y}`]
      : [`${point.x},${baselinePoints[index - 1].y}`, `${point.x},${point.y}`]);
    baselineLine.push(`100,${baselinePoints[baselinePoints.length - 1].y}`);
    const settings = comparisonSettings(reference);
    return { key, reference, latest: points[points.length - 1], first, last, maxRate,
      baselineRate: comparisonValue(reference, 'throughput')!,
      points, line: points.map(point => `${point.x},${point.y}`).join(' '),
      baselineLine: baselineLine.join(' '),
      label: `${benchmarkDatasetLabel(reference)} · ${reference.provider_name} · ${reference.configuration['tool'] === 'logos-replay' ? 'Legacy replay' : 'GuideLLM'}`,
      runIds: ordered.map(run => run.id),
      context: `${benchmarkDatasetLabel(reference)} · ${reference.provider_name} · ${reference.sample_size} requests · ${settings['max_concurrency']} concurrent`,
    };
  }).sort((a, b) => b.last - a.last);
}
