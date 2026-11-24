import { NgTemplateOutlet } from '@angular/common';
import { Component, viewChild } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { ModelManagementService } from '../../core/services/model-management.service';
import { Model } from '../../shared/models/model.model';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';
import { ModelErrorReport } from './model-error-report';
import { NightlyBenchmarks, NightlyStatus } from './nightly-benchmarks';

@Component({
  imports: [NightlyBenchmarks, NgTemplateOutlet],
  template: `
    <app-nightly-benchmarks #worker [pairId]="31" providerName="Worker A" [selected]="true" />
    <ng-container [ngTemplateOutlet]="worker.details()" />
  `,
})
class NightlySettingsHost {
  readonly worker = viewChild.required(NightlyBenchmarks);
}

describe('Nightly benchmark management', () => {
  let control: NightlyBenchmarks;
  let http: HttpTestingController;
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    control = TestBed.runInInjectionContext(() => new NightlyBenchmarks());
    control.pairId = 31;
    control.providerName = 'Worker A';
    control.state.set({ worker_ready: true, enabled: false, status: 'disabled', report: [], recovery_required: false,
      window: { start: '05:00', end: '08:00', benchmark_start_cutoff: '08:00', timezone: 'Europe/Berlin' } });
  });
  afterEach(() => { control.ngOnDestroy(); http.verify(); });

  it('labels the rotation status and distinguishes worker recovery', async () => {
    const fixture = TestBed.createComponent(NightlyBenchmarks);
    fixture.componentRef.setInput('pairId', control.pairId);
    fixture.componentRef.setInput('providerName', control.providerName);
    fixture.detectChanges();
    const ready = control.state()!;
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush(ready);
    await fixture.whenStable();
    fixture.detectChanges();

    const status: HTMLElement = fixture.nativeElement.querySelector('.nightly__status');
    expect(status.textContent.trim()).toBe('Disabled');
    expect(status.querySelector('.status-dot--gray')).not.toBeNull();

    fixture.componentInstance.state.set({ ...ready, enabled: true, status: 'scheduled' });
    fixture.detectChanges();
    expect(status.textContent.trim()).toBe('Enabled');
    expect(status.querySelector('.status-dot--gray, .status-dot--warn')).toBeNull();

    fixture.componentInstance.state.update(current => ({ ...current!, recovery_required: true }));
    fixture.detectChanges();
    expect(status.textContent).toMatch(/Enabled\s*· Recovery pending/);
    expect(status.querySelector('.status-dot--warn')).not.toBeNull();
  });

  it('shows the workload inline and enables scheduling after applying the selection', async () => {
    const fixture = TestBed.createComponent(NightlySettingsHost);
    fixture.detectChanges();
    const worker = fixture.componentInstance.worker();
    const dataset = { id: 'a'.repeat(64), name: 'Saved requests', request_count: 100, provenance: 'production' };
    const available = { ...control.state()!, workloads: [dataset] };
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush(available);
    await fixture.whenStable();
    fixture.detectChanges();

    const section: HTMLElement = fixture.nativeElement.querySelector('section');
    const workload = section.querySelector<HTMLSelectElement>('#nightly-workload-31')!;
    const toggle = section.querySelector<HTMLButtonElement>('.nightly__toggle')!;
    expect(workload.labels?.[0].textContent).toBe('Request workload');
    expect(workload.closest('details')).toBeNull();
    expect(toggle.disabled).toBe(false);

    workload.value = dataset.id;
    workload.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(toggle.disabled).toBe(true);
    const save = vi.spyOn(worker, 'saveSource');
    const apply = Array.from(section.querySelectorAll('button')).find(button => button.textContent.trim() === 'Apply dataset')!;
    apply.click();
    const applied = { ...available, workload_id: dataset.id };
    const configure = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    expect(configure.request.body.workload_id).toBe(dataset.id);
    configure.flush(applied);
    await Promise.resolve();
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush(applied);
    await save.mock.results[0].value;
    fixture.detectChanges();
    expect(worker.selectedWorkload).toBe(dataset.id);
    expect(workload.value).toBe(dataset.id);
    expect(toggle.disabled).toBe(false);
  });

  it('changes the dataset with the saved dataset as its compare-and-set expectation', async () => {
    control.selectedWorkload = 'a'.repeat(64);
    control.sourceEdited = true;
    const operation = control.saveSource();
    const request = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    expect(request.request.body).toMatchObject({ enabled: false, expected_enabled: false,
      workload_id: 'a'.repeat(64), expected_workload_id: null });
    request.flush({ enabled: false, status: 'disabled', report: [], recovery_required: false, workload_id: 'a'.repeat(64) });
    await Promise.resolve();
    http.expectOne('/api/logosdb/model_benchmarks/nightly/status').flush({ enabled: false, status: 'disabled', report: [], recovery_required: false, workload_id: 'a'.repeat(64) });
    await operation;
    expect(control.sourceEdited).toBe(false);
  });

  it('defaults to the latest 100 logged requests and keeps the saved dataset available if applying it conflicts', async () => {
    control.selectedWorkload = 'recent';
    control.sourceEdited = true;
    await control.toggleNightly();
    expect(control.confirming()).toBe(false);
    const operation = control.saveSource();
    const create = http.expectOne('/api/logosdb/model_benchmarks/workloads/create');
    expect(create.request.body).toEqual({ model_provider_id: 31, lookback_days: null });
    const dataset = { id: 'a'.repeat(64), name: 'Recent requests', request_count: 100, provenance: 'production' };
    create.flush(dataset);
    await Promise.resolve();
    const apply = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    expect(apply.request.body).toMatchObject({ workload_id: dataset.id, expected_workload_id: null, enabled: false });
    apply.flush({ detail: 'State changed' }, { status: 409, statusText: 'Conflict' });
    await operation;
    expect(control.error()).toBe('State changed');
    expect(control.state()?.workloads).toEqual([dataset]);
    expect(control.selectedWorkload).toBe(dataset.id);
    expect(control.sourceEdited).toBe(true);
  });

  it('preserves the current selection when the window has no replayable requests', async () => {
    control.selectedWorkload = 'recent';
    control.sourceEdited = true;
    control.lookbackDays = 3;
    const operation = control.saveSource();
    const create = http.expectOne('/api/logosdb/model_benchmarks/workloads/create');
    expect(create.request.body.lookback_days).toBe(3);
    create.flush({ detail: 'No replayable requests' },
      { status: 400, statusText: 'Bad Request' });
    await operation;
    http.expectNone('/api/logosdb/model_benchmarks/nightly/configure');
    expect(control.error()).toBe('No replayable requests');
    expect(control.state()?.enabled).toBe(false);
  });

  it('does not replace a workload while worker recovery is pending', async () => {
    control.state.update(value => ({ ...value!, recovery_required: true }));
    await control.saveSource();
    http.expectNone('/api/logosdb/model_benchmarks/nightly/configure');
  });

  it('keeps dataset changes unavailable when the deployment freezes one user’s recent requests', async () => {
    control.state.update(value => ({ ...value!, workload_policy: { kind: 'recent_user', username: 'source.user', request_count: 50 } }));
    expect(control.automaticSource).toBe(true);
    await control.saveSource();
    http.expectNone('/api/logosdb/model_benchmarks/nightly/configure');
  });

  it('requires a connected restart-capable worker, a window and completed recovery', async () => {
    const ready = control.state()!;
    for (const unavailable of [{ window: undefined }, { worker_ready: false }, { recovery_required: true }]) {
      control.state.set({ ...ready, ...unavailable });
      expect(control.enableBlocker).toBeTruthy();
      await control.toggleNightly();
      http.expectNone('/api/logosdb/model_benchmarks/nightly/configure');
    }
    control.state.set(ready);
    control.selectWorkload('recent');
    expect(control.enableBlocker).toContain('Apply');
    control.selectWorkload(null);
    expect(control.enableBlocker).toBeNull();
  });

  it('enables in one click and only confirms disabling', async () => {
    const pending = control.toggleNightly();
    expect(control.confirming()).toBe(false);
    const enable = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    expect(enable.request.body).toEqual({ model_provider_id: 31, enabled: true, expected_enabled: false });
    enable.flush({ enabled: true, status: 'scheduled', report: [], recovery_required: false });
    await pending;
    await control.toggleNightly();
    expect(control.confirming()).toBe(true);
    http.expectNone('/api/logosdb/model_benchmarks/nightly/configure');
    const stop = control.confirmDisable();
    const disable = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    expect(disable.request.body).toEqual({ model_provider_id: 31, enabled: false, expected_enabled: true });
    disable.flush({ enabled: false, status: 'disabled', report: [], recovery_required: false });
    await stop;
    expect(control.state()!.enabled).toBe(false);
  });

  it('shows the server reason if activation fails', async () => {
    const pending = control.toggleNightly();
    http.expectOne('/api/logosdb/model_benchmarks/nightly/configure').flush({ detail: 'State changed' }, { status: 409, statusText: 'Conflict' });
    await pending;
    expect(control.state()!.enabled).toBe(false);
    expect(control.error()).toBe('State changed');
    expect(control.confirming()).toBe(false);
    expect(control.saving()).toBe(false);
  });

  it('submits once while saving and rejects stale polling state', async () => {
    const refresh = control.refresh();
    const poll = http.expectOne('/api/logosdb/model_benchmarks/nightly/status');
    const pending = control.toggleNightly();
    await control.toggleNightly();
    const request = http.expectOne('/api/logosdb/model_benchmarks/nightly/configure');
    request.flush({ enabled: true, status: 'scheduled', report: [], recovery_required: false });
    await pending;
    poll.flush({ enabled: false, status: 'disabled', report: [], recovery_required: false });
    await refresh;
    expect(control.state()!.enabled).toBe(true);
  });

  it('uses the server cutoff in status labels', () => {
    expect(control.statusLabel('window_closed')).toContain('08:00');
    expect(control.statusLabel('window_closed_below_half')).toContain('08:00');
  });

  it('distinguishes waiting for GPU capacity from a round deferred to another window', () => {
    expect(control.statusLabel('waiting_for_capacity')).toBe('Waiting for GPU capacity');
    expect(control.statusLabel('capacity_deferred')).toBe('Deferred until another maintenance window');
  });
});

describe('Nightly worker selection', () => {
  async function openOverview(runs: ModelProviderBenchmark[] = []) {
    TestBed.configureTestingModule({ providers: [
      provideRouter([]),
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: ModelManagementService, useValue: {
        getBenchmarkWorkerLimits: async () => ({ gpu_count: 1, current: {} }),
        getBenchmarkWorkloads: async () => [],
        getBenchmarkDatasetMetadata: async () => ({ dataset: 'openai/gsm8k', subset: 'main', split: 'test',
          splits: [{ subset: 'main', split: 'test' }], text_columns: ['question'] }),
      } },
    ] });
    const http = TestBed.inject(HttpTestingController);
    const init = vi.spyOn(ModelErrorReport.prototype, 'ngOnInit').mockResolvedValue();
    const fixture = TestBed.createComponent(ModelErrorReport);
    const component = fixture.componentInstance;
    component.model.set({ id: 1, name: 'Test model' } as Model);
    component.loading.set(false);
    component.activeTab.set('performance');
    component.benchmarkPairs.set([31, 32, 33].map(id => ({
      model_provider_id: id, provider_id: id, provider_name: `Worker ${id}`, provider_type: 'logosnode',
      model_id: 1, model_name: 'Test model', endpoint_configured: true, authentication_configured: false,
    })));
    component.benchmarkAvailability.set(new Map([[31, 'online'], [32, 'online'], [33, 'offline']]));
    component.performance.set(runs);
    fixture.detectChanges();
    init.mockRestore();

    const ready = { worker_ready: true, enabled: true, status: 'scheduled', report: [], recovery_required: false,
      window: { start: '05:00', end: '08:00', benchmark_start_cutoff: '08:00', timezone: 'Europe/Berlin' } };
    const incompleteTelemetry = { status: 'active', available: true, maintenance: false, configuration: { max_num_seqs: 5 } };
    const statuses = new Map<number, NightlyStatus>([
      [31, ready],
      [32, { ...ready, enabled: false, status: 'disabled', deployment: incompleteTelemetry }],
      [33, { ...ready, worker_ready: false, status: 'unavailable',
        deployment: { status: 'offline' } }],
    ]);
    async function flushWorkerStatuses() {
      for (const request of http.match('/api/logosdb/model_benchmarks/nightly/status')) {
        request.flush(statuses.get(request.request.body.model_provider_id)!);
      }
      await fixture.whenStable();
      fixture.detectChanges();
    }

    await flushWorkerStatuses();
    return { fixture, http, flushWorkerStatuses };
  }

  it('shows three worker statuses and only the selected worker’s settings', async () => {
    const { fixture, http } = await openOverview();
    const overview: HTMLElement = fixture.nativeElement.querySelector('.nightly-overview');
    const tiles = Array.from(overview.querySelectorAll<HTMLButtonElement>('app-nightly-benchmarks button'));
    expect(tiles).toHaveLength(3);
    expect(tiles[0].textContent).toContain('Enabled');
    expect(tiles[1].textContent).toContain('Disabled');
    expect(tiles[2].textContent).toMatch(/Offline/i);
    expect(overview.querySelector('.nightly__toggle')).toBeNull();

    tiles[0].click();
    fixture.detectChanges();
    expect(tiles[0].getAttribute('aria-expanded')).toBe('true');
    expect(overview.querySelector('#nightly-workload-31')).not.toBeNull();

    tiles[1].click();
    fixture.detectChanges();
    expect(tiles[0].getAttribute('aria-expanded')).toBe('false');
    expect(tiles[1].getAttribute('aria-expanded')).toBe('true');
    expect(overview.querySelector('#nightly-workload-31')).toBeNull();
    expect(overview.querySelector('#nightly-workload-32')).not.toBeNull();

    tiles[1].click();
    fixture.detectChanges();
    expect(tiles[1].getAttribute('aria-expanded')).toBe('false');
    expect(overview.querySelector('.nightly__toggle')).toBeNull();
    http.verify();
  });

  it('defaults to an online measured worker and switches the deployment and timeline together', async () => {
    const measurement = (id: number, worker: number, baseline: number): ModelProviderBenchmark => ({
      id, model_provider_id: worker, provider_id: worker, provider_name: `Worker ${worker}`,
      model_id: 1, model_name: 'Test model', dataset: 'requests/demo', sample_size: 10,
      configuration: {
        workload: { id: 'demo', request_count: 10 },
        nightly: { baseline_id: baseline },
        benchmark: { strategy: { max_concurrency: 4 } },
        measurement: { ended_at: `2026-10-21T04:0${id}:00Z` },
      },
      metrics: {
        request_totals: { successful: 10, incomplete: 0, errored: 0, total: 10 },
        aggregate_output_tokens_per_second: 100 + id,
      },
      recorded_at: '2026-10-21T04:00:00Z',
    });
    const { fixture, http, flushWorkerStatuses } = await openOverview([
      measurement(1, 32, 1), measurement(2, 32, 1),
      measurement(3, 33, 3), measurement(4, 33, 3),
    ]);
    const root: HTMLElement = fixture.nativeElement;
    const workers = root.querySelectorAll<HTMLButtonElement>('.overview-worker-tabs button');
    const deployed = () => root.querySelector('#performance-deployment app-deployed-configuration');
    const deploymentNote = () => root.querySelector('#performance-timeline figcaption [role="status"]')?.textContent?.trim();
    const timelineRunIds = () => Array.from(root.querySelectorAll<HTMLAnchorElement>('#performance-timeline .timeline-run'),
      link => new URLSearchParams(link.search).get('benchmark'));

    expect(workers).toHaveLength(3);
    expect(workers[1].getAttribute('aria-pressed')).toBe('true');
    expect(deployed()?.textContent).toContain('Worker 32');
    expect(timelineRunIds()).toEqual(['1', '2']);
    expect(deploymentNote()).toBe('Live benchmark unverified · Batching limit not reported');
    expect(root.querySelector('#performance-timeline .timeline-run--deployed')).toBeNull();

    workers[2].click();
    fixture.detectChanges();
    await flushWorkerStatuses();
    expect(workers[2].getAttribute('aria-pressed')).toBe('true');
    expect(deployed()?.textContent).toContain('Worker 33');
    expect(timelineRunIds()).toEqual(['3', '4']);
    expect(deploymentNote()).toBeUndefined();
    http.verify();
  });
});
