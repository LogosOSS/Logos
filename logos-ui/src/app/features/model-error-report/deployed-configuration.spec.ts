import { deployedMeasurement, deployedResult, DeploymentSnapshot } from './deployed-configuration';
import { benchmarkResultRow } from './benchmark-result-row';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';

const deployment: DeploymentSnapshot = { available: true, maintenance: false, series_key: 'series', configuration: { max_num_seqs: 4, max_num_batched_tokens: 2048 } };
function run(id: number): ModelProviderBenchmark {
  return { id, model_provider_id: 1, model_id: 1, provider_id: 1, model_name: 'Model', provider_name: 'Worker',
    recorded_at: `2026-11-0${id}T00:00:00Z`, dataset: 'requests/a', sample_size: 1,
    configuration: { serving: { ...deployment.configuration, command: 'vllm serve' }, nightly: { series_key: 'series', baseline_id: 1 } },
    metrics: { request_totals: { successful: 1, total: 1, errored: 0, incomplete: 0 }, aggregate_output_tokens_per_second: 29 } };
}

describe('Current deployment measurement', () => {
  it('matches captured settings and environment, independently of a historical winner', () => {
    const old = run(1), latest = run(2), other = run(3);
    other.configuration['serving'] = { max_num_seqs: 8 };
    expect(deployedMeasurement(deployment, [old, latest, other])?.id).toBe(2);
    expect(deployedMeasurement({ ...deployment, configuration: { max_num_seqs: 16 } }, [old])).toBeNull();
    expect(deployedMeasurement({ ...deployment, series_key: 'different-worker-version' }, [old])).toBeNull();
    expect(deployedMeasurement({ ...deployment, series_key: 'different-worker-version' }, [old], false)?.id).toBe(1);
  });
  it('does not claim an offline, temporary or incomplete measurement as deployed performance', () => {
    expect(deployedMeasurement({ ...deployment, available: false }, [run(1)])).toBeNull();
    expect(deployedMeasurement({ ...deployment, maintenance: true }, [run(1)])).toBeNull();
    const incomplete = run(1); incomplete.metrics.request_totals.incomplete = 1;
    expect(deployedMeasurement(deployment, [incomplete])).toBeNull();
  });
  it('shows concrete baseline throughput and zero change for the baseline itself', () => {
    const baseline = run(1), variant = run(2); variant.metrics.aggregate_output_tokens_per_second = 31;
    expect(benchmarkResultRow(baseline, [baseline, variant])).toMatchObject({ before: 29, after: 29, delta: 0, isBaseline: true });
    expect(benchmarkResultRow(variant, [baseline, variant]).delta).toBeCloseTo(6.89655);
    expect(benchmarkResultRow(variant, [variant])).toMatchObject({ before: null, delta: null, comparisonNote: 'Baseline #1 is no longer available' });
  });
});


describe('Manual deployment comparison', () => {
  it('uses the first comparable dataset baseline without inventing a same-round baseline', () => {
    const baseline = run(1), manual = run(2);
    for (const row of [baseline, manual]) {
      row.configuration['tool'] = 'guidellm';
      row.configuration['workload'] = { id: 'frozen', request_count: 1 };
      row.configuration['benchmark'] = { profile: { kind: 'concurrent' }, strategy: { max_concurrency: 4 } };
      row.configuration['measurement'] = { ended_at: row.recorded_at };
    }
    delete manual.configuration['nightly'];
    manual.metrics.aggregate_output_tokens_per_second = 31;
    expect(deployedResult(manual, [baseline, manual])).toMatchObject({ startingBaseline: true, before: 29, baselineId: 1 });
    expect(deployedResult(manual, [baseline, manual]).delta).toBeCloseTo(6.89655);
    expect(deployedResult(manual, [manual])).toMatchObject({ startingBaseline: false, before: null });
    manual.configuration['workload'] = { id: 'other', request_count: 1 };
    expect(deployedResult(manual, [baseline, manual])).toMatchObject({ startingBaseline: false, before: null });
  });
});
