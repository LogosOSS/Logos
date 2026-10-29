import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { comparisonValue } from './benchmark-comparison';
import { batchingSignature, comparisonSettings } from './benchmark-isolation';

interface ProgressPoint {
  run: ModelProviderBenchmark;
  key?: string;
  time: number;
  gain: number;
}

/** One best completed variant per baseline, separated by captured test conditions. */
export function benchmarkProgress(runs: readonly ModelProviderBenchmark[]) {
  const byId = new Map(runs.map(run => [run.id, run]));
  const groups = new Map<string, { reference: ModelProviderBenchmark; points: Map<number, ProgressPoint> }>();
  for (const run of runs) {
    const nightly = run.configuration['nightly'] as { baseline_id?: number; reference_id?: number; eligible_for_selection?: boolean } | undefined;
    const fixed = run.configuration['tool'] === 'logos-replay';
    if (fixed && nightly?.eligible_for_selection !== true) continue;
    const baselineId = fixed ? nightly?.reference_id : nightly?.baseline_id;
    const baseline = baselineId === undefined ? undefined : byId.get(baselineId);
    if (!baseline || (!fixed && baseline.id === run.id)) continue;
    const signature = batchingSignature(run);
    if (signature === null || signature !== batchingSignature(baseline)) continue;
    const key = signature + (fixed ? ':' + baseline.id : '');
    const before = comparisonValue(baseline, 'throughput');
    const after = comparisonValue(run, 'throughput');
    const ended = (run.configuration['measurement'] as { ended_at?: string } | undefined)?.ended_at;
    const time = ended ? Date.parse(ended) : NaN;
    if (before === null || before <= 0 || after === null || !Number.isFinite(time)) continue;
    const gain = (after / before - 1) * 100;
    if (!Number.isFinite(gain)) continue;
    const group = groups.get(key) ?? { reference: baseline, points: new Map<number, ProgressPoint>() };
    const iteration = fixed ? nightly!.baseline_id! : baseline.id;
    if (!Number.isSafeInteger(iteration)) continue;
    const previous = group.points.get(iteration);
    if (!previous || gain > previous.gain || (gain === previous.gain && run.id > previous.run.id)) {
      group.points.set(iteration, { run, time, gain, key: `${iteration}` });
    }
    groups.set(key, group);
  }
  return [...groups.entries()].map(([key, group]) => {
    let points = [...group.points.values()].sort((a, b) => a.time - b.time || a.run.id - b.run.id);
    const fixed = group.reference.configuration['tool'] === 'logos-replay';
    if (fixed) {
      const measured = group.reference.configuration['measurement'] as { ended_at?: string } | undefined;
      const time = Date.parse(measured?.ended_at ?? group.reference.recorded_at);
      if (!Number.isFinite(time)) return null;
      let best: ProgressPoint = { run: group.reference, time, gain: 0, key: 'reference' };
      points = [best, ...points.filter(point => point.time >= time && point.run.id !== group.reference.id).map(point => {
        if (point.gain > best.gain) best = point;
        return { ...point, run: best.run, gain: best.gain };
      })];
    }
    const first = points[0].time, last = points[points.length - 1].time;
    const low = Math.min(0, ...points.map(point => point.gain));
    const high = Math.max(0, ...points.map(point => point.gain));
    const padding = Math.max(1, (high - low) * .15);
    const min = low - padding, max = high + padding;
    const y = (gain: number) => (max - gain) / (max - min) * 100;
    const plotted = points.map(point => ({ ...point,
      x: first === last ? 50 : (point.time - first) / (last - first) * 100, y: y(point.gain) }));
    const settings = comparisonSettings(group.reference);
    return { key, fixed, first, last, min, max, zero: y(0), points: plotted,
      line: plotted.map(point => `${point.x},${point.y}`).join(' '),
      label: `${group.reference.provider_name} · ${group.reference.dataset} · #${group.reference.id}`,
      context: fixed ? `${group.reference.provider_name} · fixed reference #${group.reference.id} · ${group.reference.sample_size} saved requests · source output limits` : `${group.reference.provider_name} · ${group.reference.dataset} · ${settings['max_concurrency']} parallel · ≤${settings['max_output_tokens']} output tokens · ${group.reference.sample_size} requests`,
    };
  }).filter((group): group is NonNullable<typeof group> => group !== null).sort((a, b) => b.last - a.last);
}
