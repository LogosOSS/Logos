import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { NodeModels } from './node-models';

const model = (overrides: Partial<NodeModelInsight> = {}): NodeModelInsight => ({
  model_name: 'org/a',
  reported: true,
  row: true,
  source: 'ui',
  configured: true,
  calibration: { status: 'calibrated', reason: null, results: [] },
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

describe('NodeModels', () => {
  let fixture: ComponentFixture<NodeModels>;
  let component: NodeModels;
  let getNodeInsights: ReturnType<typeof vi.fn>;

  const create = async (result: NodeInsights | { failure: unknown }): Promise<void> => {
    getNodeInsights = vi.fn();
    if ('failure' in result) {
      getNodeInsights.mockRejectedValue(result.failure);
    } else {
      getNodeInsights.mockResolvedValue(result);
    }
    TestBed.configureTestingModule({
      imports: [NodeModels],
      providers: [provideRouter([]), { provide: ProviderManagementService, useValue: { getNodeInsights } }],
    });
    fixture = TestBed.createComponent(NodeModels);
    fixture.componentRef.setInput('providerId', 4);
    component = fixture.componentInstance;
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };

  const text = (): string => (fixture.nativeElement as HTMLElement).textContent ?? '';

  beforeEach(() => TestBed.resetTestingModule());

  it('loads the insights of its node and lists every model', async () => {
    await create(insights([model(), model({ model_name: 'org/b', configured: false })]));

    expect(getNodeInsights).toHaveBeenCalledWith(4);
    const rows = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('a.table-row'));
    expect(rows.map((r) => r.querySelector('.provider-name')?.textContent)).toEqual(['org/a', 'org/b']);
    expect(rows[0].getAttribute('href')).toBe('/providers/4/models?model=org%2Fa');
    expect(text()).toContain('Configured');
    expect(text()).toContain('Not configured');
  });

  it('shows whether the worker is online', async () => {
    await create(insights([model()], false));
    expect(text()).toContain('Worker offline');

    TestBed.resetTestingModule();
    await create(insights([model()], true));
    expect(text()).toContain('Worker online');
  });

  it('says so when the node has no models', async () => {
    await create(insights([]));
    expect(text()).toContain('No models yet.');
  });

  it('shows the reason and a retry when the node cannot be read, and loads again on retry', async () => {
    await create({ failure: { error: { error: 'Provider 4 not found' } } });
    expect(text()).toContain('Provider 4 not found');

    getNodeInsights.mockResolvedValue(insights([model()]));
    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('button.retry')!.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(getNodeInsights).toHaveBeenCalledTimes(2);
    expect(text()).toContain('org/a');
    expect(component.error()).toBeNull();
  });

  it('falls back to a general message for an error without a body', async () => {
    await create({ failure: Object.assign(new Error(), { message: '' }) });
    expect(component.error()).toBe('The models of this node could not be loaded.');
  });

  describe('calibration label', () => {
    beforeEach(async () => create(insights([])));

    it('names calibrated, outdated and not calibrated models', () => {
      expect(component.calibrationLabel(model())).toEqual({ text: 'Calibrated', tone: 'ok', title: null });
      expect(
        component.calibrationLabel(model({ calibration: { status: 'outdated', reason: null, results: [] } })),
      ).toEqual({ text: 'Calibration outdated', tone: 'gray', title: null });
      expect(
        component.calibrationLabel(model({ calibration: { status: 'not_calibrated', reason: null, results: [] } })),
      ).toEqual({ text: 'Not calibrated', tone: 'gray', title: null });
    });

    it('shows an unsupported model in red with its reason as the tooltip', () => {
      const label = component.calibrationLabel(
        model({ calibration: { status: 'unsupported', reason: 'too-big', results: [] } }),
      );
      expect(label).toEqual({ text: 'Unsupported', tone: 'err', title: 'too-big' });
    });

    it('shows a node that calibrates as calibrating unless the model is already calibrated', () => {
      const pending = { status: 'not_calibrated' as const, reason: null, results: [] };
      expect(component.calibrationLabel(model({ calibration: pending, calibrating: true })).text).toBe('Calibrating…');
      expect(component.calibrationLabel(model({ calibrating: true })).text).toBe('Calibrated');
    });

    it('offers tuning only for a comparable benchmark that placed the model differently', () => {
      const run = (settings: Record<string, unknown>) => ({
        benchmark_id: 2,
        throughput: 50,
        settings,
        recorded_at: null,
      });
      const withBenchmark = (settings: Record<string, unknown>, comparable = true): NodeModelInsight =>
        model({
          benchmark: {
            best_eligible: run(settings),
            best_observed: run(settings),
            current: { tensor_parallel_size: 2, max_model_len: 8192 },
            eligible_for_comparison: comparable,
          },
        });

      expect(component.calibrationLabel(withBenchmark({ tensor_parallel_size: 4 })).text).toBe(
        'Calibrated · tuning available',
      );
      expect(component.hasTuning(withBenchmark({ tensor_parallel_size: 2, max_model_len: 8192 }))).toBe(false);
      expect(component.hasTuning(withBenchmark({ tensor_parallel_size: 4 }, false))).toBe(false);
      expect(component.hasTuning(withBenchmark({ block_size: 16 }))).toBe(false);
      expect(component.hasTuning(model())).toBe(false);
    });
  });
});
