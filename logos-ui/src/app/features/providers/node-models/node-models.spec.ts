import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelManagementService } from '../../../core/services/model-management.service';
import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { NodeModels } from './node-models';

const model = (overrides: Partial<NodeModelInsight> = {}): NodeModelInsight => ({
  model_name: 'org/a',
  reported: true,
  row: true,
  source: 'ui',
  configured: true,
  placement: { tensor_parallel_size: 4, kv_cache_memory_bytes: '4G', max_model_len: 32768 },
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
  let service: Record<string, ReturnType<typeof vi.fn>>;
  let getModels: ReturnType<typeof vi.fn>;

  const create = async (result: NodeInsights | { failure: unknown }): Promise<void> => {
    getNodeInsights = vi.fn();
    if ('failure' in result) {
      getNodeInsights.mockRejectedValue(result.failure);
    } else {
      getNodeInsights.mockResolvedValue(result);
    }
    getModels = vi.fn().mockResolvedValue([{ name: 'org/a' }, { name: 'org/z' }, { name: 'org/m' }]);
    service = {
      getNodeInsights,
      getNodeConfig: vi.fn().mockResolvedValue({ provider_id: 4, revision: 3, yaml_imported: true, entries: [] }),
      replaceNodeConfig: vi.fn().mockResolvedValue({ provider_id: 4, config_revision: 4 }),
    };
    TestBed.configureTestingModule({
      imports: [NodeModels],
      providers: [
        provideRouter([]),
        { provide: ProviderManagementService, useValue: service },
        { provide: ModelManagementService, useValue: { getModels } },
      ],
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
    await create(insights([model(), model({ model_name: 'org/b', configured: false, placement: null })]));

    expect(getNodeInsights).toHaveBeenCalledWith(4);
    const rows = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('a.table-row'));
    expect(rows.map((r) => r.querySelector('.provider-name')?.textContent)).toEqual(['org/a', 'org/b']);
    expect(rows[0].getAttribute('href')).toBe('/providers/4/models?model=org%2Fa');
    expect(text()).toContain('Set');
    expect(text()).toContain('Automatic');
    expect(rows[0].textContent).toContain('TP 4');
    expect(rows[1].textContent).not.toContain('TP');
  });

  it('keeps the list short for a model without a placement that a lane runs', async () => {
    const running = { tensor_parallel_size: 1, kv_cache_memory_bytes: '3G', max_model_len: 16368 };
    await create(insights([model({ configured: false, placement: null, running })]));

    expect(text()).toContain('Automatic');
    expect(text()).not.toContain('runs with');
  });

  it('says that a model without a calibration is not loadable and that an outdated one keeps running', async () => {
    const none = { status: 'not_calibrated' as const, reason: null, results: [] };
    const outdated = { status: 'outdated' as const, reason: null, results: [] };
    const unsupported = { status: 'unsupported' as const, reason: 'Needs more memory.', results: [] };
    await create(
      insights([
        model({ model_name: 'org/a', configured: false, placement: null, calibration: none }),
        model({ model_name: 'org/b', configured: false, placement: null, calibration: outdated }),
        model({ model_name: 'org/c', configured: false, placement: null, calibration: unsupported }),
        model({ model_name: 'org/d' }),
      ]),
    );

    const cells = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('[data-label="Placement"] .status'));
    expect(cells.map((c) => c.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      'Not loadable',
      'Automatic',
      'Not loadable',
      expect.stringContaining('Set'),
    ]);
    expect(cells[1].getAttribute('title')).toContain('keeps running');
    expect(cells[2].getAttribute('title')).toBe('Needs more memory.');
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
  });

  describe('connecting a model', () => {
    it('offers the catalog models the node does not list yet, sorted', async () => {
      await create(insights([model()]));

      await component.openConnect();

      expect(component.connectOpen()).toBe(true);
      expect(component.connectOptions().map((o) => o.value)).toEqual(['org/m', 'org/z']);
      expect(component.catalogLoading()).toBe(false);
    });

    it('adds the model without a placement and shows the new list', async () => {
      await create(insights([model()]));
      await component.openConnect();
      component.connectChoice.set('org/z');
      getNodeInsights.mockResolvedValue(insights([model(), model({ model_name: 'org/z', configured: false })]));

      await component.submitConnect();
      fixture.detectChanges();

      expect(service['replaceNodeConfig']).toHaveBeenCalledWith(4, 3, [
        { model_name: 'org/z', capability: {}, overrides: {} },
      ]);
      expect(component.connectOpen()).toBe(false);
      expect(text()).toContain('org/z');
    });

    it('keeps the dialog open and says why the worker refused', async () => {
      await create(insights([model()]));
      await component.openConnect();
      component.connectChoice.set('org/z');
      service['replaceNodeConfig'].mockRejectedValue({ status: 503, error: { error: 'Worker not connected' } });

      await component.submitConnect();

      expect(component.connectOpen()).toBe(true);
      expect(component.connectError()).toBe('Worker not connected');
      expect(component.connecting()).toBe(false);
    });

    it('does nothing without a choice, and cannot close or repeat while it runs', async () => {
      await create(insights([model()]));
      await component.openConnect();

      await component.submitConnect();
      expect(service['replaceNodeConfig']).not.toHaveBeenCalled();

      let finish: (value: unknown) => void = () => undefined;
      service['replaceNodeConfig'].mockReturnValue(new Promise((resolve) => (finish = resolve)));
      component.connectChoice.set('org/z');
      const first = component.submitConnect();
      component.closeConnect();
      void component.submitConnect();
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(component.connectOpen()).toBe(true);
      expect(service['replaceNodeConfig']).toHaveBeenCalledTimes(1);
      finish({ provider_id: 4, config_revision: 4 });
      await first;
      expect(component.connectOpen()).toBe(false);
    });

    it('tells when the catalog cannot be loaded and when it has nothing left to add', async () => {
      await create(insights([model()]));
      getModels.mockRejectedValueOnce(new Error('down'));
      await component.openConnect();
      expect(component.connectError()).toBe('The model catalog could not be loaded.');

      getModels.mockResolvedValue([{ name: 'org/a' }]);
      await component.openConnect();
      expect(component.connectOptions()).toEqual([]);
      expect(component.connectError()).toBeNull();
    });

    it('closes without saving', async () => {
      await create(insights([model()]));
      await component.openConnect();

      component.closeConnect();

      expect(component.connectOpen()).toBe(false);
      expect(service['replaceNodeConfig']).not.toHaveBeenCalled();
    });

    it('is switched off while the worker is offline', async () => {
      await create(insights([model()], false));

      const button = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find((b) =>
        b.textContent?.includes('Connect model'),
      )!;

      expect(button.disabled).toBe(true);
      expect(button.getAttribute('title')).toContain('must be online');
    });
  });

  it('links to the node settings of the node', async () => {
    await create(insights([model()]));

    const link = Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('a')).find((a) =>
      a.textContent?.includes('Node settings'),
    )!;

    expect(link.getAttribute('href')).toBe('/providers/4/node-settings');
  });
});
