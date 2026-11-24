import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { DeployedConfiguration, deployedMeasurement, deployedResult, DeploymentSnapshot } from './deployed-configuration';
import { benchmarkResultRow } from './benchmark-result-row';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';

const deployment: DeploymentSnapshot = { available: true, maintenance: false, series_key: 'series', configuration: { max_num_seqs: 4, max_num_batched_tokens: 2048 } };
function run(id: number): ModelProviderBenchmark {
  return { id, model_provider_id: 1, model_id: 1, provider_id: 1, model_name: 'Model', provider_name: 'Worker',
    recorded_at: `2026-11-0${id}T00:00:00Z`, dataset: 'requests/a', sample_size: 1,
    configuration: { serving: { ...deployment.configuration, command: 'vllm serve' }, nightly: { series_key: 'series', baseline_id: 1 } },
    metrics: { request_totals: { successful: 1, total: 1, errored: 0, incomplete: 0 }, aggregate_output_tokens_per_second: 29 } };
}

describe('Deployment status on Overview', () => {
  it('shows both batching limits when telemetry omits one and keeps the benchmark match unverified', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(DeployedConfiguration);
    fixture.componentRef.setInput('pairId', 1);
    fixture.componentRef.setInput('providerName', 'Worker');
    fixture.componentRef.setInput('runs', [run(1)]);
    fixture.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush({
      deployment: { ...deployment, configuration: { max_num_seqs: 4 } },
      optimizer: { production_adoption: { benchmark_id: 1, baseline_id: 1, baseline_throughput: 28, throughput: 29 } },
    });
    await Promise.resolve();
    fixture.detectChanges();
    const content: HTMLElement = fixture.nativeElement;
    const limits = () => Array.from(content.querySelectorAll('.key-parameters > div'), row =>
      [row.querySelector('dt')?.textContent, row.querySelector('dd')?.textContent].join(' '));

    expect(limits()).toEqual(['Max sequences 4', 'Max batched tokens Not reported']);
    expect(fixture.componentInstance.measurement()).toBeNull();
    const lastAdoption = content.querySelector('section[aria-label="Last confirmed adoption"]')!;
    expect(lastAdoption.textContent).toContain('Last confirmed adoption');
    expect(lastAdoption.textContent).toMatch(/28\.0\s*→.*29\.0 tok\/s/);
    expect(lastAdoption.textContent).toContain('Sequences 4 · batch tokens 2048');
    expect(lastAdoption.querySelector('a')?.getAttribute('href')).toContain('benchmark=1');

    fixture.componentInstance.state.update(state => ({
      ...state!, deployment: { ...deployment, configuration: { max_num_seqs: 0, max_num_batched_tokens: 2048 } },
    }));
    fixture.detectChanges();
    expect(limits()).toEqual(['Max sequences Auto', 'Max batched tokens 2048']);
    expect(fixture.componentInstance.measurement()).toBeNull();

    fixture.componentInstance.state.update(state => ({ ...state!, deployment }));
    fixture.detectChanges();
    expect(limits()).toEqual(['Max sequences 4', 'Max batched tokens 2048']);
    expect(fixture.componentInstance.measurement()?.id).toBe(1);
    expect(content.querySelector('section[aria-label="Last confirmed adoption"]')).toBeNull();
    fixture.destroy();
    http.verify();
  });

  it('uses worker telemetry for the status and shows sleeping lane settings separately', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(DeployedConfiguration);
    fixture.componentRef.setInput('pairId', 31);
    fixture.componentRef.setInput('providerName', 'Worker');
    fixture.componentRef.setInput('runs', []);
    fixture.detectChanges();
    const component = fixture.componentInstance;
    expect(component.workerStatus()).toBe('Checking…');
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush({
      deployment: { available: false, maintenance: false, status: 'sleeping', configuration: {},
        configured_settings: { max_num_seqs: 4 } },
    });
    await Promise.resolve();
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Model sleeping');
    expect(component.configuredParameters().map(item => item.key)).toContain('max_num_seqs');
    expect(component.parameters()).toHaveLength(0);
    const set = (update: Partial<DeploymentSnapshot>) => component.state.update(state => ({
      ...state!, deployment: { ...deployment, ...update },
    }));
    set({ active_replicas: 2 });
    expect(component.workerStatus()).toBe('Active · 2 replicas');
    set({});
    expect(component.workerStatus()).toBe('Active');
    set({ maintenance: true });
    expect(component.workerStatus()).toBe('Benchmark maintenance');
    set({ available: false, status: 'offline' });
    expect(component.workerStatus()).toBe('Worker offline');
    set({ available: false });
    expect(component.workerStatus()).toBe('No live status');
    component.error.set(true);
    expect(component.workerStatus()).toBe('Status check failed');
    fixture.destroy();
    http.verify();
  });

  it('keeps selection details collapsed and lists only runs with exclusion reasons', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(DeployedConfiguration);
    fixture.componentRef.setInput('pairId', 1);
    fixture.componentRef.setInput('providerName', 'Worker');
    fixture.componentRef.setInput('runs', [run(1)]);
    fixture.detectChanges();
    const http = TestBed.inject(HttpTestingController);
    const eligible = { benchmark_id: 1, baseline_id: 1, throughput: 29, issues: [] };
    const excluded = { benchmark_id: 2, baseline_id: 1, throughput: 31,
      issues: ['4 answer lengths differ by more than 20% from baseline', 'p95 increased'], length_mismatches: 4 };
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush({
      deployment,
      selection: { best_observed: excluded, best_eligible: eligible, comparisons: [excluded, eligible] },
    });
    await Promise.resolve();
    fixture.detectChanges();
    const content: HTMLElement = fixture.nativeElement;
    expect(content.querySelector<HTMLDetailsElement>('.deployment-details')?.open).toBe(false);
    expect(content.querySelector<HTMLDetailsElement>('.excluded-runs')?.open).toBe(false);
    expect(content.querySelectorAll('.exclusion')).toHaveLength(1);
    expect(Array.from(content.querySelectorAll('.exclusion__reasons li'), reason => reason.textContent?.trim())).toEqual(excluded.issues);
    const details = content.querySelector<HTMLDetailsElement>('.answer-length-details')!;
    expect(details.open).toBe(false);
    http.expectNone(request => request.body.length_comparison_id !== undefined);
    details.open = true;
    details.dispatchEvent(new Event('toggle'));
    const comparison = http.expectOne('/api/logosdb/model_benchmarks/nightly/status');
    expect(comparison.request.body).toEqual({ model_provider_id: 1, length_comparison_id: 2 });
    comparison.flush({ selection: { comparisons: [{ ...excluded, length_changes: [
      { request_number: 1, baseline_tokens: 100, trial_tokens: 72, change_percent: -28 },
      { request_number: 2, baseline_tokens: 100, trial_tokens: 140, change_percent: 40 },
      { request_number: 3, baseline_tokens: 0, trial_tokens: 5, change_percent: null },
      { request_number: 5, baseline_tokens: 50000, trial_tokens: 60001, change_percent: 20.002 },
    ] }] } });
    await Promise.resolve();
    fixture.detectChanges();
    const rows = Array.from(details.querySelectorAll('tbody tr'), entry =>
      Array.from(entry.querySelectorAll('th, td'), cell => cell.textContent?.trim().replace(/\s+/g, ' ')));
    expect(rows).toEqual([
      ['#1', '100', '72', 'Shorter -28.0%'],
      ['#2', '100', '140', 'Longer +40.0%'],
      ['#3', '0', '5', 'Longer Zero baseline'],
      ['#5', '50,000', '60,001', 'Longer +20.002%'],
    ]);
    fixture.destroy();
    http.verify();
  });

  it('deduplicates length detail requests and lets a failed request be retried', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(DeployedConfiguration);
    fixture.componentRef.setInput('pairId', 1);
    fixture.componentRef.setInput('providerName', 'Worker');
    fixture.componentRef.setInput('runs', []);
    fixture.detectChanges();
    const component = fixture.componentInstance;
    const http = TestBed.inject(HttpTestingController);
    const status = '/api/logosdb/model_benchmarks/nightly/status';
    http.expectOne(status).flush({ deployment });
    await Promise.resolve();
    expect(component.excludedComparisons()).toEqual([]);

    const loading = component.loadAnswerLengthDetails(2);
    const request = http.expectOne(status);
    await component.loadAnswerLengthDetails(2);
    http.expectNone(status);
    request.flush({}, { status: 503, statusText: 'Service unavailable' });
    await loading;
    expect(component.answerLengthDetails().get(2)).toBe('error');

    const retry = component.loadAnswerLengthDetails(2);
    http.expectOne(status).flush({ selection: { comparisons: [] } });
    await retry;
    expect(component.answerLengthDetails().get(2)).toEqual([]);
    await component.loadAnswerLengthDetails(2);
    http.expectNone(status);
    fixture.destroy();
    http.verify();
  });

  it('discards length details that arrive after switching workers', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])] });
    const fixture = TestBed.createComponent(DeployedConfiguration);
    fixture.componentRef.setInput('pairId', 1);
    fixture.componentRef.setInput('providerName', 'Worker');
    fixture.componentRef.setInput('runs', []);
    fixture.detectChanges();
    const component = fixture.componentInstance;
    const http = TestBed.inject(HttpTestingController);
    const status = '/api/logosdb/model_benchmarks/nightly/status';
    http.expectOne(status).flush({ deployment });

    const oldDetails = component.loadAnswerLengthDetails(2);
    const oldRequest = http.expectOne(status);
    fixture.componentRef.setInput('pairId', 4);
    fixture.detectChanges();
    const newRequest = http.expectOne(status);
    expect(newRequest.request.body).toEqual({ model_provider_id: 4 });
    newRequest.flush({ deployment });
    oldRequest.flush({ selection: { comparisons: [{ benchmark_id: 2, length_changes: [] }] } });
    await oldDetails;
    expect(component.answerLengthDetails().has(2)).toBe(false);
    fixture.destroy();
    http.verify();
  });
});

describe('Current deployment measurement', () => {
  it('matches captured settings and environment, independently of a historical winner', () => {
    const old = run(1), latest = run(2), other = run(3);
    other.configuration['serving'] = { max_num_seqs: 8 };
    expect(deployedMeasurement(deployment, [old, latest, other])?.id).toBe(2);
    expect(deployedMeasurement({ ...deployment, configuration: { max_num_seqs: 16 } }, [old])).toBeNull();
    expect(deployedMeasurement({ ...deployment, series_key: 'different-worker-version' }, [old])).toBeNull();
    expect(deployedMeasurement({ ...deployment, series_key: 'different-worker-version' }, [old], false)?.id).toBe(1);
  });
  it('does not claim an offline, mixed, temporary or incomplete measurement as deployed performance', () => {
    expect(deployedMeasurement({ ...deployment, available: false }, [run(1)])).toBeNull();
    expect(deployedMeasurement({ ...deployment, maintenance: true }, [run(1)])).toBeNull();
    expect(deployedMeasurement({ ...deployment, mixed_configuration: true }, [run(1)])).toBeNull();
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
