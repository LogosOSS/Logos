import { TestBed } from '@angular/core/testing';
import { ModelManagementService } from '../../core/services/model-management.service';
import { BenchmarkSettingsEditor } from './benchmark-settings-editor';
import { DEFAULT_BENCHMARK_SETTINGS, settingsFromBenchmark } from './benchmark-settings';
import { buildBatch, Sweep } from './benchmark-batch';
import { ModelProviderBenchmark } from '../../shared/models/provider.model';

const workload = { id: 'a'.repeat(64), name: 'Frozen coding requests', request_count: 10, provenance: 'synthetic' };
const pair = { model_provider_id: 116, provider_type: 'logosnode', provider_name: 'Worker' };
const metadata = { dataset: 'openai/gsm8k', subset: 'main', split: 'test',
  splits: [{ subset: 'main', split: 'test' }], text_columns: ['question'] };

describe('Saved request benchmark selection', () => {
  let service: { getBenchmarkWorkloads: ReturnType<typeof vi.fn>; getBenchmarkDatasetMetadata: ReturnType<typeof vi.fn>;
    getBenchmarkWorkerLimits: ReturnType<typeof vi.fn> };
  beforeEach(() => {
    service = { getBenchmarkWorkloads: vi.fn().mockResolvedValue([workload]),
      getBenchmarkDatasetMetadata: vi.fn().mockResolvedValue(metadata),
      getBenchmarkWorkerLimits: vi.fn().mockResolvedValue({ gpu_count: 1, current: {} }) };
    TestBed.configureTestingModule({ providers: [{ provide: ModelManagementService, useValue: service }] });
  });

  it.each(['logos-replay', 'guidellm'])('reopens %s saved requests without fetching Hugging Face or changing its concurrent load', async (tool) => {
    const draft = settingsFromBenchmark({ dataset: 'requests/' + workload.id, configuration: {
      tool, workload, benchmark: { profile: { kind: 'concurrent' }, strategy: { max_concurrency: 4 } },
    } } as unknown as ModelProviderBenchmark);
    expect(draft.workload_id).toBe(workload.id);
    expect(draft.dataset).toBe('openai/gsm8k');
    expect(draft.concurrency).toBe(4);
    const fixture = TestBed.createComponent(BenchmarkSettingsEditor);
    const valid: boolean[] = [];
    fixture.componentInstance.validChange.subscribe(value => valid.push(value));
    fixture.componentRef.setInput('pair', pair);
    fixture.componentRef.setInput('settings', draft);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(service.getBenchmarkDatasetMetadata).not.toHaveBeenCalled();
    expect(service.getBenchmarkWorkloads).toHaveBeenCalledWith(116);
    expect(valid.at(-1)).toBe(true);
    expect(fixture.nativeElement.textContent).toContain('10 saved requests');
    expect(fixture.nativeElement.textContent).not.toContain('Prompt column');
  });

  it('uses the full saved request count and ignores an older Hugging Face metadata response', async () => {
    let resolve!: (value: typeof metadata) => void;
    service.getBenchmarkDatasetMetadata.mockReturnValue(new Promise(r => { resolve = r; }));
    const fixture = TestBed.createComponent(BenchmarkSettingsEditor);
    const counts: number[] = [];
    fixture.componentInstance.sampleSizeChange.subscribe(value => counts.push(value));
    fixture.componentRef.setInput('pair', pair);
    fixture.detectChanges();
    await Promise.resolve();
    fixture.componentInstance.selectWorkload(workload.id);
    resolve({ ...metadata, dataset: 'other/dataset' });
    await fixture.whenStable();
    fixture.detectChanges();
    expect(counts).toEqual([10]);
    expect(fixture.componentInstance.settings().workload_id).toBe(workload.id);
    expect(fixture.componentInstance.settings().dataset).toBe('openai/gsm8k');
    expect(fixture.componentInstance.loading()).toBe(false);
  });

  it('blocks a missing saved dataset instead of falling back to Hugging Face', async () => {
    service.getBenchmarkWorkloads.mockResolvedValue([]);
    const fixture = TestBed.createComponent(BenchmarkSettingsEditor);
    const valid: boolean[] = [];
    fixture.componentInstance.validChange.subscribe(value => valid.push(value));
    fixture.componentRef.setInput('pair', pair);
    fixture.componentRef.setInput('settings', { ...DEFAULT_BENCHMARK_SETTINGS, workload_id: workload.id });
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(valid.at(-1)).toBe(false);
    expect(fixture.componentInstance.settings().workload_id).toBe(workload.id);
    expect(service.getBenchmarkDatasetMetadata).not.toHaveBeenCalled();
  });

  it('preserves the saved workload in every batch configuration and rejects ignored input sweeps', () => {
    const settings = { ...DEFAULT_BENCHMARK_SETTINGS, workload_id: workload.id, profile: 'concurrent' as const, concurrency: 4 };
    const sweep: Sweep = { key: 'max_num_seqs', mode: 'values', values: '4,8', start: 4, end: 8, step: 1 };
    const batch = buildBatch(settings, 10, [sweep], 1, { gpu_count: 1, gpu_memory_bytes: null, current: {} }, true);
    expect(batch.configurations.map(row => [row.workload_id, row.samples, row.concurrency]))
      .toEqual([[workload.id, 10, 4], [workload.id, 10, 4]]);
    for (const key of ['samples', 'seed', 'max_output_tokens']) {
      expect(() => buildBatch(settings, 10, [{ ...sweep, key }], 1, null, true)).toThrow('preserve their request count');
    }
  });
});
