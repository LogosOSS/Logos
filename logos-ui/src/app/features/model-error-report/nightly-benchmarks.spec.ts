import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { NightlyBenchmarks } from './nightly-benchmarks';

describe('Nightly benchmark management', () => {
  let control: NightlyBenchmarks;
  let http: HttpTestingController;
  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    control = TestBed.runInInjectionContext(() => new NightlyBenchmarks());
    control.managing.set(true);
    control.pairId = 31;
    control.providerName = 'Worker A';
    control.state.set({ worker_ready: true, enabled: false, status: 'disabled', report: [], recovery_required: false,
      window: { start: '05:00', end: '08:00', benchmark_start_cutoff: '08:00', timezone: 'Europe/Berlin' } });
  });
  afterEach(() => { control.ngOnDestroy(); http.verify(); });

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
    expect(control.managing()).toBe(false);
    control.managing.set(true);
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

  it('keeps the dialog open with the server reason if activation fails', async () => {
    const pending = control.toggleNightly();
    http.expectOne('/api/logosdb/model_benchmarks/nightly/configure').flush({ detail: 'State changed' }, { status: 409, statusText: 'Conflict' });
    await pending;
    expect(control.state()!.enabled).toBe(false);
    expect(control.error()).toBe('State changed');
    expect(control.managing()).toBe(true);
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
});
