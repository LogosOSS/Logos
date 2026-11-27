import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { NavigationEnd, Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { filter, firstValueFrom } from 'rxjs';
import { ModelManagementService } from '../../core/services/model-management.service';
import { Model } from '../../shared/models/model.model';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { benchmarkProgress, recordConfiguration } from './benchmark-progress';
import { BenchmarkResults } from './benchmark-results';
import { ModelDetails } from './model-details';

beforeEach(() => TestBed.configureTestingModule({ providers: [provideRouter([])] }));

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

  it('shows the linked run in a dialog without expanding history or adding a table row', async () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const baseline = run(1, 40, 1), selected = run(2, 50, 1);
    fixture.componentRef.setInput('runs', [baseline, selected, ...Array.from({ length: 8 }, (_, i) => run(i + 3, 20))]);
    fixture.componentRef.setInput('selectedRunId', 2);
    fixture.detectChanges();
    await fixture.whenStable();
    const result = document.querySelector('[role="dialog"]');
    expect(result?.textContent).toContain('Benchmark #2');
    expect(result?.textContent).toContain('Sequences: 4 → 8');
    expect(result?.textContent).toContain('+25.0%');
    expect(fixture.nativeElement.querySelector('.run-history').open).toBe(false);
    expect(fixture.nativeElement.querySelectorAll('tbody tr:not(.dataset-heading)')).toHaveLength(5);
  });

  it('keeps a dialog open with an explanation if the selected benchmark disappears', async () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 40)]);
    fixture.componentRef.setInput('selectedRunId', 1);
    fixture.detectChanges();
    await fixture.whenStable();

    fixture.componentRef.setInput('runs', []);
    fixture.detectChanges();
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('This benchmark is no longer available.');
  });
  it('starts collapsed and preserves the selected order when reopened', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', [run(1, 50), run(2, 40)]);
    fixture.detectChanges();
    const history = fixture.nativeElement.querySelector('.run-history') as HTMLDetailsElement;
    const toggle = history.querySelector('summary')!;
    expect(history.open).toBe(false);
    expect(history.querySelector('.top-configurations h3')?.textContent).toBe('Highest recorded throughput');
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

describe('Benchmark detail navigation', () => {
  it('renders the model before the compact benchmark history arrives', async () => {
    TestBed.configureTestingModule({ providers: [
      provideRouter([{ path: 'models/:id/details', component: ModelDetails }]),
      provideHttpClient(),
      provideHttpClientTesting(),
    ] });
    const harness = await RouterTestingHarness.create('/models/1/details');
    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/api/logosdb/get_model').flush({ id: 1, name: 'Model' });
    await Promise.resolve();
    harness.detectChanges();

    const history = http.expectOne('/api/logosdb/model_benchmarks');
    expect(history.request.body).toEqual({ model_id: 1, summary_only: true });
    expect(harness.routeNativeElement?.querySelector('h1')?.textContent).toContain('Model');
    expect(harness.routeNativeElement?.querySelector('[aria-label="Loading performance"] .pi-spinner')).not.toBeNull();
    expect(harness.routeNativeElement?.textContent).not.toContain('No benchmark results available');

    history.flush({ benchmarks: [], pairs: [], runs: [] });
    http.expectOne('/api/logosdb/get_model_calibration_logs').flush({ logs: [] });
    http.expectOne('/api/logosdb/get_model_prices').flush({ providers: [] });
    await harness.fixture.whenStable();
    harness.detectChanges();
    expect(harness.routeNativeElement?.textContent).toContain('No benchmark results available');
  });

  it('opens direct links and preserves the worker, timeline, history and other URL parameters on close', async () => {
    const resultUrl = '/models/1/details?tab=performance&view=fixtures';
    TestBed.configureTestingModule({ providers: [
      provideRouter([{ path: 'models/:id/details', component: ModelDetails }]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ModelManagementService, useValue: {
        getBenchmarkWorkerLimits: async () => ({ gpu_count: 1, current: {} }),
        getBenchmarkWorkloads: async () => [],
        getBenchmarkDatasetMetadata: async () => ({ dataset: 'openai/gsm8k', subset: 'main', split: 'test',
          splits: [{ subset: 'main', split: 'test' }], text_columns: ['question'] }),
      } },
    ] });
    const fetchModel = vi.spyOn(ModelDetails.prototype, 'fetchModel').mockImplementation(async function(this: ModelDetails) {
      this.model.set({ id: 1, name: 'Model' } as Model);
      this.performance.set([
        measured(1, 40, 1, 21), measured(2, 50, 1, 22),
        ...[measured(3, 80, 3, 21), measured(4, 90, 3, 22)]
          .map(row => ({ ...row, model_provider_id: 2, provider_id: 2, provider_name: 'Worker 2' })),
      ]);
      this.benchmarkPairs.set([1, 2].map(id => ({
        model_provider_id: id, provider_id: id, provider_name: `Worker ${id}`, provider_type: 'logosnode',
        model_id: 1, model_name: 'Model', endpoint_configured: true, authentication_configured: false,
      })));
      this.benchmarkAvailability.set(new Map([[1, 'online'], [2, 'online']]));
      this.loading.set(false);
    });
    const harness = await RouterTestingHarness.create(`${resultUrl}&benchmark=4#results`);
    fetchModel.mockRestore();
    const http = TestBed.inject(HttpTestingController);
    for (const request of http.match('/api/logosdb/model_benchmarks/nightly/status')) {
      request.flush({ enabled: false, status: 'disabled', recovery_required: false });
    }
    http.expectOne('/api/logosdb/request_latency_stats').flush({ range: { start: '', end: '' }, rows: [] });
    await harness.fixture.whenStable();
    const router = TestBed.inject(Router);
    async function navigateWith(action: () => void) {
      const navigation = firstValueFrom(router.events.pipe(filter(event => event instanceof NavigationEnd)));
      action();
      await navigation;
      harness.detectChanges();
      await harness.fixture.whenStable();
    }

    const view = harness.routeNativeElement!;
    const dialog = () => document.querySelector('[role="dialog"]');
    const selectedWorker = () => view.querySelector('.overview-worker-tabs [aria-pressed="true"]')?.textContent?.trim();
    const history = view.querySelector<HTMLDetailsElement>('.run-history')!;
    const timeline = view.querySelector<HTMLElement>('.timeline-scroll')!;
    expect(dialog()?.textContent).toContain('Benchmark #4');
    expect(selectedWorker()).toBe('Worker 2');
    expect(history.open).toBe(false);

    const dialogClose = dialog()!.querySelector('button')!;
    await navigateWith(() => dialogClose.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(dialog()).toBeNull();
    expect(router.url).toBe(`${resultUrl}#results`);
    expect(selectedWorker()).toBe('Worker 2');

    history.open = true;
    timeline.scrollLeft = 40;
    const historyLink = history.querySelector<HTMLAnchorElement>('a[href*="benchmark=2"]')!;
    historyLink.focus();
    await navigateWith(() => historyLink.click());
    expect(dialog()?.textContent).toContain('Benchmark #2');
    expect(router.url).toBe(`${resultUrl}&benchmark=2#results`);
    expect(selectedWorker()).toBe('Worker 2');
    expect(history.open).toBe(true);
    expect(timeline.scrollLeft).toBe(40);

    const closeButton = Array.from(dialog()!.querySelectorAll('button')).find(button => button.textContent?.trim() === 'Close')!;
    await navigateWith(() => closeButton.click());
    expect(dialog()).toBeNull();
    expect(router.url).toBe(`${resultUrl}#results`);
    expect(history.open).toBe(true);
    expect(document.activeElement).toBe(historyLink);
    http.verify();
  });
});

describe('Nightly progress', () => {
  it('keeps slower runs and compares each measurement with its own round baseline', () => {
    const rows = [measured(3, 42, 3, 22), measured(4, 44, 3, 22), measured(5, 39, 3, 22),
      measured(1, 40, 1, 21), measured(2, 38, 1, 21)];
    const chart = benchmarkProgress(rows)[0];
    expect(chart.reference.id).toBe(3);
    expect(chart.points.map(point => point.run.id)).toEqual([1, 2, 3, 4, 5]);
    expect(chart.points.map(point => point.gain)).toEqual([0, expect.closeTo(-5), 0,
      expect.closeTo(100 * (44 / 42 - 1)), expect.closeTo(100 * (39 / 42 - 1))]);
    expect(chart.points.filter(point => point.isBaseline).map(point => point.run.id)).toEqual([1, 3]);
    expect(chart.ticks).toEqual([0, 10, 20, 30, 40, 50]);
    expect(chart.axisMax).toBe(50);
    expect(benchmarkProgress([...rows].reverse())[0].runIds).toEqual(chart.runIds);
  });

  it('keeps actual dates and configurations across worker releases and daily baselines', () => {
    const first = measured(1, 40, 1, 21), record = measured(2, 45, 1, 22), current = measured(3, 42, 3, 23);
    first.configuration['nightly'] = { baseline_id: 1, series_key: 'old-release' };
    record.configuration['nightly'] = { baseline_id: 1, series_key: 'old-release' };
    current.configuration['nightly'] = { baseline_id: 3, reference_id: 3, series_key: 'new-release' };
    const chart = benchmarkProgress([current, record, first])[0];
    expect(chart.points.map(point => [point.run.id, point.rate])).toEqual([[1, 40], [2, 45], [3, 42]]);
    expect(chart.reference.id).toBe(3);
    expect(chart.points[1].time).toBe(Date.parse('2026-10-22T05:10:00Z'));
    expect(recordConfiguration(chart.points[1].run)).toContain('Sequences 8');
    expect(chart.last).toBe(Date.parse('2026-10-23T05:10:00Z'));
  });

  it('shows manual measurements without inventing a baseline comparison', () => {
    const first = measured(1, 40, 1, 21), manual = measured(2, 38, 1, 22);
    delete manual.configuration['nightly'];
    const points = benchmarkProgress([first, manual])[0].points;
    expect(points.map(point => point.run.id)).toEqual([1, 2]);
    expect(points[1].gain).toBeNull();
    expect(points[1].baselineId).toBeUndefined();
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
  });

  it('gives simultaneous measurements separate links and keeps the same scale when older runs open', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const runs = Array.from({ length: 8 }, (_, i) => measured(i + 1, i === 0 ? 100 : 50, 1, 21));
    fixture.componentRef.setInput('runs', runs);
    fixture.detectChanges();
    const timeline = fixture.nativeElement.querySelector('.benchmark-progress');
    const links = () => [...timeline.querySelectorAll('.timeline-run')] as HTMLAnchorElement[];
    expect(links()).toHaveLength(6);
    expect(links().map(link => link.querySelector('.timeline-id')?.textContent)).toEqual(['#3', '#4', '#5', '#6', '#7', '#8']);
    expect(links()[5].getAttribute('aria-label')).toContain('Trial #8');
    expect(links()[5].getAttribute('href')).toContain('benchmark=8');
    expect(links()[0].querySelector<HTMLElement>('.timeline-stem')?.style.height).toBe('50%');
    timeline.querySelector('.timeline-toggle').click();
    fixture.detectChanges();
    expect(links()).toHaveLength(8);
    expect(links()[0].getAttribute('aria-label')).toContain('Baseline #1');
    expect(links()[0].querySelector<HTMLElement>('.timeline-stem')?.style.height).toBe('100%');
    timeline.querySelector('.timeline-toggle').click();
    fixture.detectChanges();
    expect(links()).toHaveLength(6);
    fixture.componentRef.setInput('runs', [runs[1]]);
    fixture.detectChanges();
    expect(links()).toHaveLength(0);
    expect(timeline.textContent).toContain('complete baseline');
    const checks = timeline.querySelector('.timeline-policy') as HTMLDetailsElement;
    expect(checks.open).toBe(false);
    expect(checks.querySelector('summary')?.textContent).toBe('What does Logos check?');
    expect([...checks.querySelectorAll('.timeline-policy__checks strong')].map(label => label.textContent))
      .toEqual(['Matched baseline', 'Complete responses', 'Higher throughput']);
    expect(checks.querySelector('.timeline-policy__examples')?.textContent).toContain('Pass 2.1 s (+5%)');
    expect(checks.querySelector('.timeline-policy__examples')?.textContent).toContain('Fail 2.3 s (+15%)');
  });

  it('keeps the deployed benchmark visible and locatable among 50 runs', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const runs = Array.from({ length: 50 }, (_, index) => measured(index + 1, 50 + index, 1, 21));
    fixture.componentRef.setInput('runs', runs);
    fixture.componentRef.setInput('deployedBenchmarkIds', new Set([8]));
    fixture.detectChanges();

    const timeline = fixture.nativeElement.querySelector('.benchmark-progress') as HTMLElement;
    const marked = () => timeline.querySelector<HTMLAnchorElement>('.timeline-run--deployed');
    const jump = timeline.querySelector<HTMLButtonElement>('.timeline-deployment')!;
    expect(timeline.querySelectorAll('.timeline-run')).toHaveLength(6);
    expect(marked()?.textContent).toContain('#8');
    expect(marked()?.getAttribute('aria-label')).toContain('Currently deployed');
    expect(jump.textContent).toContain('Deployed · #8');

    timeline.querySelector<HTMLButtonElement>('.timeline-toggle')?.click();
    fixture.detectChanges();
    expect(timeline.querySelectorAll('.timeline-run')).toHaveLength(50);
    expect(timeline.querySelectorAll('.timeline-run--deployed')).toHaveLength(1);
    jump.click();
    expect(document.activeElement).toBe(marked());

    fixture.componentRef.setInput('deployedBenchmarkIds', new Set());
    fixture.detectChanges();
    expect(marked()).toBeNull();
    expect(timeline.querySelector('.timeline-deployment')).toBeNull();

    fixture.componentRef.setInput('lastAdoptionId', 8);
    timeline.querySelector<HTMLButtonElement>('.timeline-toggle')?.click();
    fixture.detectChanges();
    const adoption = timeline.querySelector<HTMLButtonElement>('.timeline-deployment--historical')!;
    expect(adoption.textContent).toContain('Last adoption · #8');
    expect(timeline.querySelectorAll('.timeline-run')).toHaveLength(6);
    expect(marked()).toBeNull();
    adoption.click();
    expect(document.activeElement?.getAttribute('aria-label')).toContain('Last confirmed adoption');
  });

  it('defaults to a deployment group and follows a different worker or workload selection', () => {
    const fixture = TestBed.createComponent(BenchmarkResults);
    const first = measured(1, 40, 1, 21), deployed = measured(2, 50, 1, 21);
    const newer = { ...measured(3, 45, 3, 22, 8), model_provider_id: 2, provider_id: 2, provider_name: 'Second worker' };
    fixture.componentRef.setInput('runs', [first, deployed, newer]);
    fixture.componentRef.setInput('deployedBenchmarkIds', new Set([2]));
    fixture.detectChanges();

    const content = fixture.nativeElement as HTMLElement;
    const picker = content.querySelector<HTMLSelectElement>('.timeline-dataset')!;
    expect(picker.selectedOptions[0].textContent).toContain('Deployed ·');
    expect(content.querySelector('.timeline-run--deployed')?.textContent).toContain('#2');

    picker.value = picker.options[0].value;
    picker.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(picker.selectedOptions[0].textContent).toContain('Second worker');
    expect(content.querySelector('.timeline-run--deployed')).toBeNull();

    fixture.componentRef.setInput('deployedBenchmarkIds', new Set([2, 3]));
    fixture.detectChanges();
    expect(content.querySelector('.timeline-run--deployed')?.textContent).toContain('#3');
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

  it('keeps all complete observations and marks rejected faster trials', () => {
    const groups = benchmarkProgress([replay(1, 40), replay(2, 60, 1), replay(3, 20, 3),
      replay(4, 50, 3), replay(5, 100, 3, false), replay(6, 200, 3, true, 'other')]);
    expect(groups).toHaveLength(1);
    expect(groups[0].reference.id).toBe(3);
    expect(groups[0].points.map(point => point.run.id)).toEqual([1, 2, 3, 4, 5]);
    expect(groups[0].points[3].gain).toBe(150);
    expect(groups[0].points[4].selectionNote).toContain('Not eligible');
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', groups[0].points.map(point => point.run));
    fixture.detectChanges();
    const fastest = fixture.nativeElement.querySelector('.timeline-run[href*="benchmark=5"]');
    expect(fastest.textContent).toContain('100.0');
    expect(fastest.getAttribute('aria-label')).toContain('Excluded from adoption');
  });

  it('distinguishes legacy replay from GuideLLM measurements of the same dataset', () => {
    const first = replay(1, 40), other = replay(3, 42);
    other.configuration['tool'] = tool === 'guidellm' ? 'logos-replay' : 'guidellm';
    const charts = benchmarkProgress([first, other]);
    expect(charts).toHaveLength(2);
    expect(new Set(charts.map(chart => chart.label)).size).toBe(2);
    expect(charts.map(chart => chart.label).join(' ')).toContain('GuideLLM');
    expect(charts.map(chart => chart.label).join(' ')).toContain('Legacy replay');
  });

  it('shows gains against the newly measured baseline after a configuration change', () => {
    const rows = [replay(1, 40), replay(2, 42, 1), replay(3, 44), replay(4, 41, 3)];
    const chart = benchmarkProgress(rows)[0];
    expect(chart.reference.id).toBe(3);
    expect(chart.points.map(point => point.gain)).toEqual([0, expect.closeTo(5), 0, expect.closeTo(100 * (41 / 44 - 1))]);
    const fixture = TestBed.createComponent(BenchmarkResults);
    fixture.componentRef.setInput('runs', rows);
    fixture.detectChanges();
    const links = fixture.nativeElement.querySelectorAll('.timeline-run');
    expect(links[3].textContent).toContain('-6.8%');
    expect(links[3].getAttribute('aria-label')).toContain('vs. baseline #3');
    expect(links[2].textContent).toContain('Baseline');
    expect(links[1].getAttribute('aria-label')).toContain('vs. baseline #1');
    expect(links[0].textContent).toContain('Baseline');
  });

  it('shows a completed regression instead of hiding it from the timeline', () => {
    const baseline = replay(1, 40), worse = replay(2, 38, 1);
    const chart = benchmarkProgress([worse, baseline])[0];
    expect(chart.points.map(point => point.run.id)).toEqual([1, 2]);
    expect(chart.points[1].gain).toBeCloseTo(-5);
    expect(chart.points[0].time).toBe(Date.parse('2026-10-21T04:00:00Z'));
  });
});
