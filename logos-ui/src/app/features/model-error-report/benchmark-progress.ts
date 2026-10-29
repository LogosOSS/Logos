import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { batchingSignature, comparisonSettings } from './benchmark-isolation';

interface ProgressPoint {
  run: ModelProviderBenchmark;
  key: string;
  time: number;
}

function completedAt(run: ModelProviderBenchmark): number {
  const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
  return Date.parse(ended ?? run.recorded_at);
}

/** Keep the best observed throughput against one starting baseline under matching test conditions. */
export function benchmarkProgress(runs: readonly ModelProviderBenchmark[]) {
  const byId = new Map(runs.map(run => [run.id, run]));
  const groups = new Map<string, { reference: ModelProviderBenchmark; points: Map<number, ProgressPoint> }>();
  for (const run of runs) {
    const nightly = run.configuration['nightly'] as { baseline_id?: number; reference_id?: number; eligible_for_selection?: boolean } | undefined;
    const fixed = run.configuration['tool'] === 'logos-replay';
    if (fixed && nightly?.eligible_for_selection !== true) continue;
    const baselineId = fixed ? nightly?.reference_id : nightly?.baseline_id;
    const baseline = baselineId === undefined ? undefined : byId.get(baselineId);
    if (!baseline || !Number.isFinite(completedAt(baseline))) continue;
    const signature = batchingSignature(run);
    if (signature === null || signature !== batchingSignature(baseline)) continue;
    const key = signature + (fixed ? ':' + baseline.id : '');
    const before = comparisonValue(baseline, 'throughput');
    const after = comparisonValue(run, 'throughput');
    const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
    const time = ended ? Date.parse(ended) : NaN;
    if (before === null || before <= 0 || after === null || !Number.isFinite(time)) continue;
    const group = groups.get(key) ?? { reference: baseline, points: new Map<number, ProgressPoint>() };
    if (!fixed && (completedAt(baseline) < completedAt(group.reference)
      || (completedAt(baseline) === completedAt(group.reference) && baseline.id < group.reference.id))) {
      group.reference = baseline;
    }
    const iteration = fixed ? nightly!.baseline_id! : baseline.id;
    if (!Number.isSafeInteger(iteration)) continue;
    const previous = group.points.get(iteration);
    const previousRate = previous ? comparisonValue(previous.run, 'throughput')! : null;
    const winner = previousRate === null || after > previousRate || (after === previousRate && run.id > previous!.run.id)
      ? run : previous!.run;
    group.points.set(iteration, { run: winner, time: Math.max(time, previous?.time ?? time), key: `${iteration}` });
    groups.set(key, group);
  }
  return [...groups.entries()].map(([key, group]) => {
    const fixed = group.reference.configuration['tool'] === 'logos-replay';
    const time = completedAt(group.reference);
    const referenceRate = comparisonValue(group.reference, 'throughput')!;
    if (!Number.isFinite(time)) return null;
    let best = group.reference;
    let bestRate = referenceRate;
    const points = [
      { run: best, time, gain: 0, stepGain: null as number | null, previousRunId: null as number | null, key: 'reference' },
      ...[...group.points.values()].filter(point => point.time >= time
        && (point.time > time || point.run.id !== group.reference.id))
        .sort((a, b) => a.time - b.time || a.run.id - b.run.id).map(point => {
          const previousRunId = best.id, previousRate = bestRate;
          const rate = comparisonValue(point.run, 'throughput')!;
          if (rate > bestRate) { best = point.run; bestRate = rate; }
          return { ...point, run: best, gain: (bestRate / referenceRate - 1) * 100,
            stepGain: (bestRate / previousRate - 1) * 100, previousRunId };
        }),
    ];
    const first = points[0].time, last = points[points.length - 1].time;
    const high = Math.max(0, ...points.map(point => point.gain));
    const min = 0, max = high + Math.max(1, high * .15);
    const y = (gain: number) => (max - gain) / (max - min) * 100;
    const plotted = points.map(point => ({ ...point,
      x: first === last ? 50 : (point.time - first) / (last - first) * 100, y: y(point.gain) }));
    const settings = comparisonSettings(group.reference);
    const latest = plotted[plotted.length - 1];
    const reference = group.reference;
    return { key, fixed, reference, latest, first, last, min, max, zero: y(0), points: plotted,
      line: plotted.map(point => `${point.x},${point.y}`).join(' '),
      label: `${group.reference.provider_name} · ${group.reference.dataset} · #${group.reference.id}`,
      context: fixed ? `${group.reference.provider_name} · fixed reference #${group.reference.id} · ${group.reference.sample_size} saved requests · source output limits` : `${group.reference.provider_name} · ${group.reference.dataset} · ${settings['max_concurrency']} parallel · ≤${settings['max_output_tokens']} output tokens · ${group.reference.sample_size} requests`,
    };
  }).filter((group): group is NonNullable<typeof group> => group !== null).sort((a, b) => b.last - a.last);
}
