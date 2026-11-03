import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
beforeEach(() => TestBed.configureTestingModule({ providers: [provideRouter([])] }));
import { BenchmarkResults } from './benchmark-results';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { benchmarkProgress } from './benchmark-progress';

function run(id: number, rate: number, baseline?: number, pair = 1): ModelProviderBenchmark {
  return { id, model_provider_id: pair, model_id: 1, model_name: 'Model', provider_id: pair,
    provider_name: 'Worker', dataset: 'openai/gsm8k', sample_size: 50, recorded_at: '2026-10-21T04:00:00Z',
    configuration: baseline ? { nightly: { baseline_id: baseline, parameters: { max_num_seqs: 8 }, original: { max_num_seqs: 4 } } } : {},
    metrics: { request_totals: { successful: 50, total: 50, errored: 0, incomplete: 0 },
      output_tokens_per_second: { successful: { mean: rate, percentiles: { p50: rate, p95: rate, p99: rate } } } } };
}

describe('Benchmark result baseline', () => {
  it('groups by the frozen dataset identity across references and keeps names readable', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const first = run(1, 40), later = run(2, 50), other = run(3, 30);
    first.configuration = { workload: { id: 'a', name: 'Coding pilot' }, nightly: { reference_id: 1 } };
    later.configuration = { workload: { id: 'a', name: 'Coding pilot' }, nightly: { reference_id: 2 } };
    other.configuration = { workload: { id: 'b', name: 'Coding pilot' } };
    fixture.componentRef.setInput('runs', [first, later, other]);
    fixture.detectChanges();
    expect(fixture.componentInstance.datasetGroups().map(group => group.rows.map(row => row.run.id))).toEqual([[3], [2, 1]]);
    expect(fixture.nativeElement.querySelectorAll('.dataset-heading')).toHaveLength(2);
  });

  it.each(['runId', 'measuredFrom', 'before', 'after', 'delta'] as const)('restores the initial order after three clicks on %s', key => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 40), run(2, 30), run(3, 50)]);
    const component = fixture.componentInstance;
    const initial = component.visibleRows().map(row => row.run.id);
    component.sortBy(key);
    expect(component.sort()?.direction).toBe('ascending');
    component.sortBy(key);
    expect(component.sort()?.direction).toBe('descending');
    component.sortBy(key);
    expect(component.sort()).toBeNull();
    expect(component.visibleRows().map(row => row.run.id)).toEqual(initial);
  });

  it('sorts by run and measured start time, with missing start times last', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const early = run(3, 40), late = run(1, 40), missing = run(2, 40);
    early.configuration = { measurement: { started_at: '2026-11-01T05:00:00Z' } };
    late.configuration = { measurement: { started_at: '2026-11-02T05:00:00Z' } };
    fixture.componentRef.setInput('runs', [early, late, missing]);
    const component = fixture.componentInstance;
    component.sortBy('runId');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([1, 2, 3]);
    component.sortBy('measuredFrom');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([3, 1, 2]);
    component.sortBy('measuredFrom');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([1, 3, 2]);
  });

  it('reveals an older linked run together with its results and parameter changes', async () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const baseline = run(1, 40, 1), selected = run(2, 50, 1);
    fixture.componentRef.setInput('runs', [baseline, selected, ...Array.from({ length: 8 }, (_, i) => run(i + 3, 20))]);
    fixture.componentRef.setInput('selectedRunId', 2);
    fixture.detectChanges();
    await fixture.whenStable();
    const result = fixture.nativeElement.querySelector('.selected-result');
    expect(result.textContent).toContain('Benchmark #2');
    expect(result.textContent).toContain('Sequences: 4 → 8');
    expect(result.textContent).toContain('+25.0%');
    expect(fixture.nativeElement.querySelector('.run-history').open).toBe(true);
    expect(fixture.nativeElement.querySelectorAll('.selected-run')).toHaveLength(1);
  });
  it('starts collapsed and preserves the selected order when reopened', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 50), run(2, 40)]);
    fixture.detectChanges();
    const history = fixture.nativeElement.querySelector('.run-history') as HTMLDetailsElement;
    const toggle = history.querySelector('summary')!;
    expect(history.open).toBe(false);
    toggle.click();
    expect(history.open).toBe(true);
    (history.querySelectorAll('.sortable button')[3] as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(history.querySelectorAll('th')[4].getAttribute('aria-sort')).toBe('ascending');
    expect(fixture.componentInstance.visibleRows().map(row => row.run.id)).toEqual([2, 1]);
    toggle.click();
    expect(history.open).toBe(false);
    toggle.click();
    expect(history.open).toBe(true);
    expect(fixture.componentInstance.visibleRows().map(row => row.run.id)).toEqual([2, 1]);
  });

  it('sorts all measurements before limiting the list, with invalid values last in both directions', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 100), run(2, 0), run(3, 20), run(4, 20),
      run(5, 50), run(6, NaN), run(7, Infinity)]);
    const component = fixture.componentInstance;
    component.sortBy('after');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([2, 4, 3, 5, 1]);
    component.showAll.set(true);
    expect(component.visibleRows().map(row => row.run.id)).toEqual([2, 4, 3, 5, 1, 7, 6]);
    component.sortBy('after');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([1, 5, 4, 3, 2, 7, 6]);
  });

  it('sorts baseline values and percentage gains numerically, including losses and missing baselines', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 40, 1), run(2, 20, 1), run(3, 40, 1),
      run(4, 80, 1), run(5, 100, 5), run(6, 110, 5), run(7, 50, 999)]);
    const component = fixture.componentInstance;
    component.showAll.set(true);
    component.sortBy('delta');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([2, 5, 3, 1, 6, 4, 7]);
    component.sortBy('delta');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([4, 6, 5, 3, 1, 2, 7]);
    component.sortBy('before');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([4, 3, 2, 1, 6, 5, 7]);
    component.sortBy('before');
    expect(component.visibleRows().map(row => row.run.id)).toEqual([6, 5, 4, 3, 2, 1, 7]);
  });

  it('keeps older runs available without expanding the initial result list', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', Array.from({ length: 7 }, (_, i) => run(i + 1, 40)));
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('tbody tr:not(.dataset-heading)')).toHaveLength(5);
    fixture.nativeElement.querySelector('.results-more').click();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('tbody tr:not(.dataset-heading)')).toHaveLength(7);
  });

  it('compares only the explicitly linked baseline on the same deployment', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 40, 1), run(2, 50, 1), run(3, 60), run(4, 80, 1, 2)]);
    const rows = fixture.componentInstance.rows();
    expect(rows.find(row => row.run.id === 2)?.delta).toBe(25);
    expect(rows.find(row => row.run.id === 1)?.before).toBe(40);
    expect(rows.find(row => row.run.id === 3)?.before).toBeNull();
    expect(rows.find(row => row.run.id === 4)?.before).toBeNull();
  });

  it('shows requested manual changes without inventing a throughput baseline', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const manual = run(5, 40);
    manual.configuration = { requested_overrides: { max_num_seqs: 8 } };
    fixture.componentRef.setInput('runs', [manual]);
    expect(fixture.componentInstance.rows()[0].changes).toEqual(['Sequences → 8']);
    expect(fixture.componentInstance.rows()[0].before).toBeNull();
  });

  it('leaves a deleted baseline and historical measurement times unknown', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(2, 50, 1)]);
    expect(fixture.componentInstance.rows()[0].before).toBeNull();
    expect(fixture.componentInstance.rows()[0].measurement).toBeUndefined();
  });
});

function measured(id: number, rate: number, baseline: number, day: number, concurrency = 4): ModelProviderBenchmark {
  const result = run(id, rate, baseline);
  result.configuration = { ...result.configuration,
    serving: { tensor_parallel_size: 1, pipeline_parallel_size: 1, max_num_seqs: id === baseline ? 4 : 8 },
    measurement: { ended_at: `2026-10-${day}T05:10:00Z` },
    scenario: { spec: { data: [{ load_kwargs: { name: 'main', split: 'test' } }], seed: { value: 42 },
      data_column_mapper: { column_mappings: { text_column: 'question' } },
      backend: { extras: { body: { max_tokens: 512 } } } } },
    benchmark: { profile: { kind: 'concurrent' }, strategy: { max_concurrency: concurrency } },
  };
  return result;
}

describe('Nightly progress', () => {
  it('starts at the earliest baseline and keeps the best result across completed nights', () => {
    const groups = benchmarkProgress([measured(3, 40, 3, 22), measured(4, 44, 3, 22), measured(5, 42, 3, 22),
      measured(1, 40, 1, 21), measured(2, 38, 1, 21)]);
    expect(groups).toHaveLength(1);
    expect(groups[0].reference.id).toBe(1);
    expect(groups[0].points.map(point => point.run.id)).toEqual([1, 4]);
    expect(groups[0].points[0].gain).toBe(0);
    expect(groups[0].points[1].gain).toBeCloseTo(10);
    expect(groups[0].min).toBeLessThan(40);
    expect(groups[0].max).toBeGreaterThan(44);
    expect(benchmarkProgress([measured(1, 40, 1, 21), measured(2, 38, 1, 21),
      measured(3, 40, 3, 22), measured(5, 42, 3, 22), measured(4, 44, 3, 22)])[0].line).toBe(groups[0].line);
  });

  it('keeps actual record dates and configurations across worker releases and daily references', () => {
    const first = measured(1, 40, 1, 21), record = measured(2, 45, 1, 22), worse = measured(3, 42, 3, 23);
    first.configuration['nightly'] = { baseline_id: 1, series_key: 'old-release' };
    record.configuration['nightly'] = { baseline_id: 1, series_key: 'old-release' };
    worse.configuration['nightly'] = { baseline_id: 3, reference_id: 3, series_key: 'new-release' };
    const chart = benchmarkProgress([worse, record, first])[0];
    expect(chart.points.map(point => [point.run.id, point.rate])).toEqual([[1, 40], [2, 45]]);
    expect(chart.points[1].time).toBe(Date.parse('2026-10-22T05:10:00Z'));
    expect(chart.points[1].configuration).toContain('Sequences 8');
    expect(chart.last).toBe(Date.parse('2026-10-23T05:10:00Z'));
  });

  it('includes complete manual records after a captured starting baseline', () => {
    const first = measured(1, 40, 1, 21), manual = measured(2, 48, 1, 22);
    delete manual.configuration['nightly'];
    expect(benchmarkProgress([first, manual])[0].points.map(point => point.run.id)).toEqual([1, 2]);
  });

  it('separates workloads and rejects changes to input generation within a comparison', () => {
    const changed = measured(7, 99, 1, 22);
    (changed.configuration['scenario'] as { spec: { seed: { value: number } } }).spec.seed.value = 99;
    const missing = measured(8, 99, 999, 22);
    const failed = measured(9, 99, 1, 22);
    failed.metrics.request_totals.errored = 1;
    const groups = benchmarkProgress([measured(1, 40, 1, 21), measured(2, 44, 1, 21),
      measured(3, 40, 3, 22, 8), measured(4, 48, 3, 22, 8), changed, missing, failed, run(10, 99, 1)]);
    expect(groups).toHaveLength(2);
    expect(groups.map(group => group.points.map(point => point.run.id))).toEqual([[3, 4], [1, 2]]);
    expect(groups.every(group => !group.line.includes('NaN'))).toBe(true);
  });

  it('renders the starting baseline and measured result and removes deleted comparisons', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [measured(1, 40, 1, 21), measured(2, 44, 1, 21)]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('.progress-point')).toHaveLength(2);
    expect(fixture.nativeElement.querySelector('polyline')).not.toBeNull();
    fixture.componentRef.setInput('runs', [measured(2, 44, 1, 21)]);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.benchmark-progress')).toBeNull();
  });
});


describe('Top measured configuration', () => {
  it('keeps independent provider records and rejects partial or invalid measurements', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const partial = run(4, 999);
    partial.metrics.request_totals.successful = 49;
    const failed = run(5, 1000);
    failed.metrics.request_totals.errored = 1;
    fixture.componentRef.setInput('runs', [run(1, 40), run(2, 50), run(3, 30, undefined, 2), partial, failed, run(6, NaN)]);
    expect(fixture.componentInstance.topRuns().map(top => top.run.id)).toEqual([2, 3]);
  });

  it('does not rank an older faster workload against the latest recorded workload', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const old = measured(1, 100, 1, 20);
    const recent = measured(2, 40, 2, 21);
    recent.dataset = 'new/dataset';
    recent.recorded_at = '2026-10-22T04:00:00Z';
    fixture.componentRef.setInput('runs', [old, recent]);
    expect(fixture.componentInstance.topRuns()[0].run.id).toBe(2);
  });

  it('shows the winning saved parameters and recalculates after removal', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const winner = run(2, 50);
    winner.configuration = { serving: { max_num_seqs: 20, enable_prefix_caching: false } };
    fixture.componentRef.setInput('runs', [run(1, 40), winner]);
    expect(fixture.componentInstance.topRuns()[0].parameters.find(item => item.key === 'max_num_seqs')?.value).toBe('20');
    expect(fixture.componentInstance.topRuns()[0].parameters.find(item => item.key === 'enable_prefix_caching')?.value).toBe('Disabled');
    fixture.componentRef.setInput('runs', [run(1, 40)]);
    expect(fixture.componentInstance.topRuns()[0].run.id).toBe(1);
  });
});


describe.each(['logos-replay', 'guidellm'])('Fixed request reference (%s)', (tool) => {
  function replay(id: number, rate: number, baseline = id, eligible = true, workload = 'a') {
    const row = run(id, rate, baseline);
    row.sample_size = 10;
    row.dataset = 'requests/' + workload;
    row.configuration = { tool, workload: { id: workload, request_count: 10 },
      nightly: { baseline_id: baseline, reference_id: 1, series_key: 'series', eligible_for_selection: eligible },
      benchmark: { profile: { kind: 'concurrent' }, strategy: { max_concurrency: 4 } },
      serving: { max_num_seqs: id === 1 ? 16 : 32, dtype: 'float16' },
      measurement: { ended_at: `2026-10-${20 + id}T04:00:00Z` } };
    row.metrics.request_totals = { successful: 10, total: 10, errored: 0, incomplete: 0 };
    row.metrics.aggregate_output_tokens_per_second = rate;
    return row;
  }

  it('keeps one reference and cumulative gains across nights without counting rejected trials', () => {
    const groups = benchmarkProgress([replay(1, 40), replay(2, 60, 1), replay(3, 20, 3),
      replay(4, 50, 3), replay(5, 100, 3, false), replay(6, 200, 3, true, 'other')]);
    expect(groups).toHaveLength(1);
    expect(groups[0].fixed).toBe(true);
    expect(groups[0].points.map(point => point.gain)).toEqual([0, 50]);
    expect(groups[0].points.map(point => point.run.id)).toEqual([1, 2]);
    expect(new Set(groups[0].points.map(point => point.key)).size).toBe(2);
  });

  it('shows total growth from the baseline and relative improvement from the previous point', () => {
    const rows = [replay(1, 40), replay(2, 42, 1), replay(3, 44), replay(4, 41)];
    const chart = benchmarkProgress(rows)[0];
    expect(chart.points.map(point => point.gain)).toEqual([0, expect.closeTo(5), expect.closeTo(10)]);
    expect(chart.points.map(point => point.stepGain)).toEqual([null, expect.closeTo(5), expect.closeTo(100 * (44 / 42 - 1))]);
    expect(chart.points.map(point => point.previousRunId)).toEqual([null, 1, 2]);
    expect(chart.points.map(point => point.run.id)).toEqual([1, 2, 3]);
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', rows);
    fixture.detectChanges();
    const points = fixture.nativeElement.querySelectorAll('.progress-point') as NodeListOf<HTMLAnchorElement>;
    expect(points[2].getAttribute('aria-label')).toContain('10.0% total vs. starting baseline, 4.8% vs. previous point');
    expect(points[2].textContent).toContain('+4.8% vs. previous point (run #2)');
    expect(points[0].textContent).toContain('Starting baseline · 0%');
  });

  it('does not invent a new observation when a round fails to improve the baseline', () => {
    const baseline = replay(1, 40), worse = replay(2, 38, 1);
    const chart = benchmarkProgress([worse, baseline])[0];
    expect(chart.points.map(point => point.gain)).toEqual([0]);
    expect(chart.points[0].time).toBe(Date.parse('2026-10-21T04:00:00Z'));
    expect(chart.points[0].run.id).toBe(1);
  });
});
