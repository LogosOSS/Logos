import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { BehaviorSubject } from 'rxjs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeConfig, NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { NodeModel } from './node-model';

const insight = (overrides: Partial<NodeModelInsight> = {}): NodeModelInsight => ({
  model_name: 'org/a',
  reported: true,
  row: true,
  source: 'ui',
  configured: true,
  calibration: {
    status: 'calibrated',
    reason: null,
    context_limit: 32768,
    results: [
      {
        tp: 4,
        active: true,
        base_residency_mb: 41200,
        loaded_vram_mb: 40000,
        kv_min_mb: 1024,
        kv_max_mb: 4096,
        pairs: [],
        cold_load_time_s: 54.6,
        wake_from_sleep_time_s: 1.3,
        enforce_eager: true,
      },
    ],
  },
  benchmark: null,
  calibrating: false,
  ...overrides,
});

const insights = (models: NodeModelInsight[], online = true): NodeInsights => ({
  provider_id: 4,
  worker: { online, supports_actions: {}, config_revision: 3 },
  gpus: [],
  models,
});

const config: NodeConfig = {
  provider_id: 4,
  revision: 3,
  yaml_imported: true,
  entries: [
    {
      model_name: 'org/a',
      capability: { tensor_parallel_size: 4, kv_cache_memory_bytes: '4G', min_context_fraction: 1 },
      overrides: { max_model_len: 32768, quantization: 'awq' },
    },
  ],
};

describe('NodeModel', () => {
  let fixture: ComponentFixture<NodeModel>;
  let service: Record<string, ReturnType<typeof vi.fn>>;
  let query: BehaviorSubject<ReturnType<typeof convertToParamMap>>;

  const create = async (model: string | null = 'org/a'): Promise<HTMLElement> => {
    query = new BehaviorSubject(convertToParamMap(model ? { model } : {}));
    TestBed.configureTestingModule({
      imports: [NodeModel],
      providers: [
        provideRouter([]),
        { provide: ProviderManagementService, useValue: service },
        {
          provide: ActivatedRoute,
          useValue: { paramMap: new BehaviorSubject(convertToParamMap({ providerId: '4' })), queryParamMap: query },
        },
      ],
    });
    fixture = TestBed.createComponent(NodeModel);
    fixture.detectChanges();
    await settle();
    return fixture.nativeElement as HTMLElement;
  };

  /** The page loads three requests at once; let them and the render finish. */
  const settle = async (): Promise<void> => {
    await fixture.whenStable();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    service = {
      getNodeConfig: vi.fn().mockResolvedValue(config),
      getNodeInsights: vi.fn().mockResolvedValue(insights([insight()])),
      getProviders: vi.fn().mockResolvedValue([{ id: 4, name: 'hochbruegge' }]),
    };
  });

  it('shows the placement and the measured values of the model', async () => {
    const root = await create();

    expect(root.querySelector('h1')?.textContent).toContain('org/a');
    expect(root.textContent).toContain('hochbruegge');
    expect(root.textContent).toContain('TP 4');
    expect(root.textContent).toContain('4 GB');
    expect(root.textContent).toContain('32,768 tokens');
    expect(root.textContent).toContain('Measured by calibration');
    expect(root.textContent).toContain('40.2 GB');
    expect(root.textContent).toContain('54.6 s');
    expect(root.textContent).toContain('Calibrated');
    expect(root.textContent).toContain('Configured');
    expect(root.textContent).toContain('Online');
  });

  it('lists what the entry sets besides the placement', async () => {
    const root = await create();

    expect(root.textContent).toContain('Advanced settings');
    expect(root.textContent).toContain('quantization');
    expect(root.textContent).toContain('min_context_fraction');
    expect(root.textContent).not.toContain('kv_cache_memory_bytes');
  });

  it('shows a model without a placement as not configured yet', async () => {
    service['getNodeConfig'].mockResolvedValue({ ...config, entries: [] });
    service['getNodeInsights'].mockResolvedValue(
      insights([insight({ row: false, configured: false, calibration: { status: 'not_calibrated', reason: null, results: [] } })]),
    );
    const root = await create();

    expect(root.textContent).toContain('Not configured yet');
    expect(root.textContent).toContain('next calibration window');
    expect(root.textContent).not.toContain('Measured by calibration');
  });

  it('tells a calibrated model without a placement to configure it', async () => {
    service['getNodeInsights'].mockResolvedValue(insights([insight({ configured: false })]));
    const root = await create();

    expect(root.textContent).toContain('calibrated but has no placement');
  });

  it('warns when the worker is offline', async () => {
    service['getNodeInsights'].mockResolvedValue(insights([insight()], false));
    const root = await create();

    expect(root.textContent).toContain('Worker offline');
    expect(root.textContent).toContain('cannot be edited');
  });

  it('points out a benchmark that placed the model differently', async () => {
    const best = { benchmark_id: 7, throughput: 52.3, settings: { tensor_parallel_size: 2, max_model_len: 16384 }, recorded_at: null };
    service['getNodeInsights'].mockResolvedValue(
      insights([
        insight({
          benchmark: {
            best_eligible: best,
            best_observed: best,
            current: { tensor_parallel_size: 4, max_model_len: 32768 },
            eligible_for_comparison: true,
          },
        }),
      ]),
    );
    const root = await create();

    expect(root.textContent).toContain('Recommendation not in use');
    expect(root.textContent).toContain('TP 2 · context 16384');
    expect(root.textContent).toContain('52.3 tok/s');
    expect(root.textContent).toContain('Running now');
    expect(root.textContent).toContain('TP 4 · context 32768');
  });

  it('shows no recommendation when the model already runs as the benchmark placed it', async () => {
    const best = { benchmark_id: 7, throughput: 52.3, settings: { tensor_parallel_size: 4 }, recorded_at: null };
    service['getNodeInsights'].mockResolvedValue(
      insights([
        insight({
          benchmark: { best_eligible: best, best_observed: best, current: { tensor_parallel_size: 4 }, eligible_for_comparison: true },
        }),
      ]),
    );
    const root = await create();

    expect(root.textContent).not.toContain('Recommendation not in use');
    expect(root.textContent).toContain('Best benchmark');
  });

  it('says so when the node does not know the model', async () => {
    service['getNodeConfig'].mockResolvedValue({ ...config, entries: [] });
    service['getNodeInsights'].mockResolvedValue(insights([]));
    const root = await create();

    expect(root.textContent).toContain('This node does not know this model.');
  });

  it('shows the error and loads again on retry', async () => {
    service['getNodeConfig'].mockRejectedValueOnce({ error: { error: 'Provider 4 not found' } });
    const root = await create();
    expect(root.textContent).toContain('Provider 4 not found');

    root.querySelector<HTMLButtonElement>('button.retry')!.click();
    await settle();

    expect(service['getNodeConfig']).toHaveBeenCalledTimes(2);
    expect(root.textContent).toContain('Measured by calibration');
  });

  it('needs a model in the address', async () => {
    const root = await create(null);

    expect(root.textContent).toContain('This page needs a node and a model.');
    expect(service['getNodeConfig']).not.toHaveBeenCalled();
  });

  it('reloads when the model in the address changes', async () => {
    await create();
    query.next(convertToParamMap({ model: 'org/other' }));
    await settle();

    expect(service['getNodeConfig']).toHaveBeenCalledTimes(2);
  });

  it('names the node by its id when it is not in the provider list', async () => {
    service['getProviders'].mockResolvedValue([]);
    const root = await create();

    expect(root.textContent).toContain('Node 4');
  });
});
