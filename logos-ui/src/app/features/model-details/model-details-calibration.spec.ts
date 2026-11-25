import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { ModelDetails } from './model-details';

function workerLog(id: number, name: string, fields: Record<string, unknown> = {}) {
  return {
    provider_id: id,
    provider_name: name,
    success: false,
    log_text: null,
    summary: null,
    stages: null,
    recorded_at: null,
    updated_at: '2026-11-25T13:00:00Z',
    ...fields,
  };
}

describe('Model details calibration', () => {
  let details: ModelDetails;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    details = TestBed.runInInjectionContext(() => new ModelDetails());
    details.modelId.set(1);
  });

  afterEach(() => {
    details.ngOnDestroy();
    http.verify();
  });

  async function loadLogs(logs: unknown[]) {
    const loading = details.fetchModel(1);
    http.expectOne('/api/logosdb/get_model').flush({ id: 1, name: 'example/model' });
    await Promise.resolve();
    http.expectOne('/api/logosdb/get_model_calibration_logs').flush({ logs });
    http.expectOne('/api/logosdb/model_benchmarks').flush({ benchmarks: [], runs: [], pairs: [] });
    http.expectOne('/api/logosdb/get_model_prices').flush({ providers: [] });
    await loading;
  }

  it('shows successful calibration even when the worker retained no raw log', async () => {
    await loadLogs([
      workerLog(1, 'CUDA worker', {
        success: true,
        recorded_at: '2026-11-25T12:05:00Z',
        summary: {
          backend: 'cuda', tensor_parallel_size: 2, gpu_devices: '0,1',
          base_residency_mb: 4096, loaded_vram_mb: 8192, sleeping_residual_mb: 512,
          cold_load_time_s: 12, wake_from_sleep_time_s: 0.5, probe_command: 'vllm serve example/model',
        },
      }),
      workerLog(2, 'Single GPU', { success: true }),
    ]);

    expect(details.hasCompleteLogs()).toBe(true);
    expect(details.hasParseableCalibrationData()).toBe(true);
    expect(details.calibrationResult()?.status).toBe('success');
    expect(details.completeLog()).toBe('');
    expect(details.selectedCalibratedAt()).toBe(details.formatBenchmarkTimestamp('2026-11-25T12:05:00Z'));
    expect(details.summaryProbeCommand()).toBe('vllm serve example/model');
    expect(details.summaryTiles().find(tile => tile.title === 'VRAM')?.rows).toContainEqual({
      label: 'Sleeping residual', value: '512 MB',
    });
    expect(details.summaryTiles().find(tile => tile.title === 'Timing')?.rows).toContainEqual({
      label: 'Wake from sleep', value: '500 ms',
    });
    const coordination = details.checklistItems().find(item => item.name === 'Multi-GPU Coordination');
    expect(coordination?.scope).toEqual({ nodes: ['CUDA worker'], totalApplicable: 1 });
    expect(details.getScopeLabel(coordination?.scope, coordination?.status)).toBe('100%');
    details.onLogSelectorChange('2');
    expect(details.selectedLog()?.node).toBe('Single GPU');
    expect(details.selectedCalibratedAt()).toBe(details.formatBenchmarkTimestamp('2026-11-25T13:00:00Z'));
    expect(details.summaryTiles()).toEqual([]);
  });

  it('groups failures by stage and retains each worker’s own log anchor', async () => {
    await loadLogs([
      workerLog(1, 'CUDA worker', {
        log_text: 'CUDA out of memory\nretry\nCUDA out of memory',
        stages: [
          { name: 'Engine Initialization', status: 'success' },
          { name: 'Weight Loading', status: 'failure', reason_kind: 'observed', reason_code: 'cuda-oom',
            log_anchor: 'CUDA out of memory' },
        ],
      }),
      workerLog(2, 'Metal worker', {
        log_text: 'starting\nmodel allocation failed',
        summary: { backend: 'metal', tensor_parallel_size: 1, metal_capacity_floor_mb: 4096 },
        stages: [
          { name: 'Weight Loading', status: 'failure', generic_error_message: 'Allocation error',
            generic_error_detail: 'Model allocation failed.', log_anchor: 'model allocation failed' },
        ],
      }),
      workerLog(3, 'Healthy worker', { success: true }),
    ]);

    const weights = details.checklistItems().find(item => item.name === 'Weight Loading');
    expect(weights).toMatchObject({
      status: 'failure', errorMessage: 'Model weights too large for GPU',
      scope: { nodes: ['CUDA worker', 'Metal worker'], totalApplicable: 3 },
    });
    expect(details.metalCapacityFloorMb()).toBe(4096);
    expect(details.getScopeLabel(weights?.scope, weights?.status)).toBe('33%');
    expect(details.getScopePercentageClass(weights?.scope, weights?.status)).toBe('scope-badge--danger');
    expect(details.getScopeDetails(weights?.scope)).toBe('CUDA worker, Metal worker');

    details.openNodeLog('CUDA worker', weights?.nodeAnchors?.get('CUDA worker'));
    expect(details.highlightedLogLineIndex()).toBe(2);
    details.openNodeLog('Metal worker', weights?.nodeAnchors?.get('Metal worker'));
    expect(details.activeTab()).toBe('complete_logs');
    expect(details.highlightedLogLineIndex()).toBe(1);
    expect(details.calibrationResult()?.probes[0]?.errorDetail).toBe('Model allocation failed.');
    expect(details.summaryTiles().map(tile => tile.title)).toContain('Unified Memory');
    expect(details.summaryTiles().find(tile => tile.title === 'Capacity')?.rows[0]?.value).toBe('4096 MB');
  });

  it('repairs a failed calibration with no failed stage and prefers node health over model incompatibility', async () => {
    await loadLogs([
      workerLog(1, 'Storage worker', {
        unsupported_reason: 'gated-repo-no-token', node_unhealthy_reason: 'filesystem-eio',
        stages: [{ name: 'Node Preflight', status: 'success' }],
      }),
      workerLog(2, 'Network worker', {
        observed_reason: 'hf-network-timeout',
        stages: [{ name: 'Model Resolution & Download', status: 'unknown' }, { name: 'Engine Initialization', status: 'unknown' }],
      }),
      workerLog(3, 'Restricted worker', { unsupported_reason: 'gated-repo-no-token' }),
      workerLog(4, 'New worker build', { observed_reason: 'new-worker-reason' }),
    ]);

    expect(details.calibrationResult()?.probes[0]).toMatchObject({
      reasonKind: 'node_unhealthy', errorMessage: 'Filesystem I/O error',
      stages: [{ name: 'Node Preflight', status: 'failure' }],
    });
    details.onLogSelectorChange('2');
    expect(details.calibrationResult()?.probes[0]?.stages).toMatchObject([
      { name: 'Model Resolution & Download', status: 'failure', logAnchor: 'Read timed out' },
      { name: 'Engine Initialization', status: 'unknown' },
    ]);
    details.onLogSelectorChange('3');
    expect(details.calibrationResult()?.probes[0]?.stages).toMatchObject([
      { name: 'Model Resolution & Download', status: 'failure', reasonKind: 'unsupported' },
    ]);
    details.onLogSelectorChange('4');
    expect(details.calibrationResult()?.probes[0]?.errorDetail).toContain('new-worker-reason');
  });

  it('ignores benign fallback messages in legacy logs and recognizes a successful retry', async () => {
    await loadLogs([
      workerLog(1, 'Legacy worker', {
        success: true,
        summary: { backend: 'cuda', tensor_parallel_size: 2 },
        log_text: [
          'Calibration probe — first attempt',
          'ERROR Cannot use FA version 2 is not supported',
          'Trying another attention backend',
          'RuntimeError: weights failed to load',
          'Calibration probe — retry',
          'non-default args:',
          'Initializing a V1 LLM engine',
          'Model loading took 5 seconds',
          'GPU KV cache size: 1234',
          'Application startup complete.',
        ].join('\n'),
      }),
      workerLog(2, 'Unknown reason', { log_text: 'A worker exited without a recognized probe or error.' }),
      workerLog(3, 'Old worker', { log_text: 'loading model\nerror: engine stopped early' }),
    ]);

    expect(details.calibrationResult()).toMatchObject({ attempts: 2, status: 'success' });
    expect(details.calibrationResult()?.probes[0]?.errorMessage).toBe('RuntimeError: weights failed to load');
    expect(details.calibrationResult()?.probes[1]?.stages).toContainEqual({ name: 'Multi-GPU Coordination', status: 'success' });
    details.onLogSelectorChange('2');
    expect(details.calibrationResult()?.probes[0]?.errorMessage).toContain('log format not recognized');
    details.onLogSelectorChange('3');
    expect(details.calibrationResult()?.probes[0]?.errorMessage).toBe('error: engine stopped early');
  });

  it('shows a worker’s error when the complete log is unavailable', async () => {
    await loadLogs([workerLog(1, 'Legacy worker', { log_text: 'RuntimeError: worker lost connection' })]);

    const download = details.downloadFullLog();
    const request = http.expectOne('/api/logosdb/get_model_calibration_log_full');
    expect(request.request.body).toEqual({ id: 1, provider_id: 1 });
    expect(details.downloadingLog()).toBe(true);
    request.error(new ProgressEvent('error'));
    await download;
    expect(details.downloadLogError()).toBe('Failed to fetch the log from the worker.');

    const retry = details.downloadFullLog();
    http.expectOne('/api/logosdb/get_model_calibration_log_full').flush({ error: 'Worker is offline' }, {
      status: 503, statusText: 'Service Unavailable',
    });
    await retry;
    expect(details.downloadLogError()).toBe('Worker is offline');
    expect(details.downloadingLog()).toBe(false);
  });
});
