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
        pairs: [
          { kv_mb: 1024, max_model_len: 5440, measured: true },
          { kv_mb: 4096, max_model_len: 32768, measured: true },
        ],
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
      replaceNodeConfig: vi.fn().mockResolvedValue({ provider_id: 4, config_revision: 4 }),
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

  describe('editing', () => {
    const click = (root: HTMLElement, selector: string): void => root.querySelector<HTMLElement>(selector)!.click();
    const edit = async (): Promise<HTMLElement> => {
      const root = await create();
      click(root, '.head button.btn-primary');
      fixture.detectChanges();
      return root;
    };
    const page = (): NodeModel => fixture.componentInstance;

    it('starts from the stored placement and keeps Save off until something changes', async () => {
      const root = await edit();

      expect(page().draft()).toEqual({ tp: 4, kvGb: 4, ctx: 32768 });
      expect(root.textContent).toContain('Editing');
      expect(root.querySelector<HTMLButtonElement>('.savebar .btn-primary')!.disabled).toBe(true);
      expect(root.textContent).toContain('No changes.');
    });

    it('keeps the KV cache inside the measured window and the context inside what that cache holds', async () => {
      const root = await edit();

      page().setKv(9);
      expect(page().draft()!.kvGb).toBe(4);
      page().setKv(1);
      fixture.detectChanges();

      expect(page().draft()).toEqual({ tp: 4, kvGb: 1, ctx: 5120 });
      expect(root.textContent).toContain('Up to about 5,120 tokens');
      expect(root.textContent).toContain('KV cache 4 → 1 GB per GPU');
      expect(root.textContent).toContain('Context 32768 → 5120');
      page().setCtx(999999);
      expect(page().draft()!.ctx).toBe(5120);
      page().setCtx(100);
      expect(page().draft()!.ctx).toBe(1024);
    });

    it('ignores input that is not a number and a TP that was not calibrated', async () => {
      await edit();

      page().setKv('abc');
      page().setCtx('abc');
      page().setTp(8);

      expect(page().draft()).toEqual({ tp: 4, kvGb: 4, ctx: 32768 });
    });

    it('saves the whole list with the revision it was read at and shows the result', async () => {
      const root = await edit();
      page().setKv(2);
      fixture.detectChanges();

      click(root, '.savebar .btn-primary');
      await settle();

      expect(service['replaceNodeConfig']).toHaveBeenCalledWith(4, 3, [
        {
          model_name: 'org/a',
          capability: { tensor_parallel_size: 4, kv_cache_memory_bytes: '2G', min_context_fraction: 1 },
          overrides: { max_model_len: 14336, quantization: 'awq' },
        },
      ]);
      expect(page().editing()).toBe(false);
      expect(root.textContent).toContain('Saved');
      expect(service['getNodeConfig']).toHaveBeenCalledTimes(2);
    });

    it('shows the checking state while the worker decides and locks the page', async () => {
      const root = await edit();
      page().setKv(2);
      fixture.detectChanges();
      let finish: (value: unknown) => void = () => undefined;
      service['replaceNodeConfig'].mockReturnValue(new Promise((resolve) => (finish = resolve)));

      click(root, '.savebar .btn-primary');
      fixture.detectChanges();

      expect(root.textContent).toContain('Checking the settings');
      expect(root.querySelector('.grid')!.hasAttribute('inert')).toBe(true);
      expect(root.querySelector<HTMLButtonElement>('.savebar .btn-secondary')!.disabled).toBe(true);
      page().cancelEdit();
      expect(page().editing()).toBe(true);

      finish({ provider_id: 4, config_revision: 4 });
      await settle();
      expect(page().editing()).toBe(false);
    });

    it('shows what the worker rejected, at the field and in the list, and keeps the input', async () => {
      const root = await edit();
      page().setCtx(8192);
      fixture.detectChanges();
      service['replaceNodeConfig'].mockRejectedValue({
        status: 400,
        error: {
          error: 'org/a: max_model_len: too long',
          errors: [{ model: 'org/a', key: 'max_model_len', message: 'too long', suggestion: null }],
        },
      });

      click(root, '.savebar .btn-primary');
      await settle();

      expect(root.textContent).toContain('Worker rejected the settings');
      expect(root.textContent).toContain('1 setting needs a change.');
      expect(root.querySelector('#ctx')!.getAttribute('aria-invalid')).toBe('true');
      expect(root.querySelector('.ferr')!.textContent).toContain('too long');
      expect(page().draft()!.ctx).toBe(8192);
      expect(page().editing()).toBe(true);
    });

    it('offers to reload after a conflict and leaves the editor with the newer list', async () => {
      const root = await edit();
      page().setKv(2);
      fixture.detectChanges();
      service['replaceNodeConfig'].mockRejectedValue({ status: 409, error: { error: 'Configuration changed meanwhile' } });

      click(root, '.savebar .btn-primary');
      await settle();
      expect(root.textContent).toContain('Configuration changed');

      click(root, '.banner .btn-secondary');
      await settle();

      expect(service['getNodeConfig']).toHaveBeenCalledTimes(2);
      expect(page().editing()).toBe(false);
      expect(page().failure()).toBeNull();
    });

    it('offers to try again when the worker cannot be reached', async () => {
      const root = await edit();
      page().setKv(2);
      fixture.detectChanges();
      service['replaceNodeConfig'].mockRejectedValueOnce({ status: 503, error: { error: 'Worker not connected' } });

      click(root, '.savebar .btn-primary');
      await settle();
      expect(root.textContent).toContain('Worker not reachable');

      click(root, '.banner .btn-danger');
      await settle();

      expect(service['replaceNodeConfig']).toHaveBeenCalledTimes(2);
      expect(page().editing()).toBe(false);
    });

    it('drops the draft on cancel', async () => {
      const root = await edit();
      page().setKv(2);

      click(root, '.savebar .btn-secondary');
      fixture.detectChanges();

      expect(page().editing()).toBe(false);
      expect(page().draft()).toBeNull();
      expect(service['replaceNodeConfig']).not.toHaveBeenCalled();
    });

    it('offers no editing while the worker is offline or the model is not calibrated', async () => {
      service['getNodeInsights'].mockResolvedValue(insights([insight()], false));
      expect((await create()).querySelector('.head button')).toBeNull();

      TestBed.resetTestingModule();
      service['getNodeInsights'].mockResolvedValue(
        insights([insight({ calibration: { status: 'not_calibrated', reason: null, results: [] } })]),
      );
      expect((await create()).querySelector('.head button')).toBeNull();
    });

    it('starts a model without a placement from the best benchmark and calls it Configure', async () => {
      const best = { benchmark_id: 7, throughput: 52.3, settings: { kv_cache_memory_bytes: '2G', max_model_len: 8192 }, recorded_at: null };
      service['getNodeConfig'].mockResolvedValue({ ...config, entries: [] });
      service['getNodeInsights'].mockResolvedValue(
        insights([
          insight({
            row: false,
            configured: false,
            benchmark: { best_eligible: best, best_observed: best, current: {}, eligible_for_comparison: true },
          }),
        ]),
      );
      const root = await create();
      expect(root.querySelector('.head button')!.textContent).toContain('Configure model');

      click(root, '.head button.btn-primary');
      fixture.detectChanges();

      expect(page().draft()).toEqual({ tp: 4, kvGb: 2, ctx: 8192 });
      expect(root.textContent).toContain('TP 4');
      expect(page().dirty()).toBe(true);
    });

    it('takes the benchmark recommendation into the editor', async () => {
      const best = { benchmark_id: 7, throughput: 52.3, settings: { kv_cache_memory_bytes: '2G', max_model_len: 8192 }, recorded_at: null };
      service['getNodeInsights'].mockResolvedValue(
        insights([
          insight({
            benchmark: { best_eligible: best, best_observed: best, current: { kv_cache_memory_bytes: '4G' }, eligible_for_comparison: true },
          }),
        ]),
      );
      const root = await create();

      click(root, '.banner .btn-secondary');
      fixture.detectChanges();

      expect(page().editing()).toBe(true);
      expect(page().draft()).toEqual({ tp: 4, kvGb: 2, ctx: 8192 });
    });
  });

  describe('removing the configuration', () => {
    const page = (): NodeModel => fixture.componentInstance;

    it('offers it for a model that has an entry, while the worker can check the list', async () => {
      const root = await create();
      expect(root.textContent).toContain('Remove configuration');
      expect(root.querySelector('.btn-remove')).not.toBeNull();

      TestBed.resetTestingModule();
      service['getNodeInsights'].mockResolvedValue(insights([insight()], false));
      expect((await create()).querySelector('.btn-remove')).toBeNull();

      TestBed.resetTestingModule();
      service['getNodeInsights'].mockResolvedValue(insights([insight({ row: false, configured: false })]));
      service['getNodeConfig'].mockResolvedValue({ ...config, entries: [] });
      expect((await create()).querySelector('.btn-remove')).toBeNull();
    });

    it('opens a confirmation and closes it again without saving', async () => {
      const root = await create();

      root.querySelector<HTMLButtonElement>('.btn-remove')!.click();
      expect(page().removeOpen()).toBe(true);
      page().closeRemove();

      expect(page().removeOpen()).toBe(false);
      expect(service['replaceNodeConfig']).not.toHaveBeenCalled();
    });

    it('saves the list without the model, keeps the calibration and says so', async () => {
      const root = await create();
      page().openRemove();
      service['getNodeConfig'].mockResolvedValue({ ...config, revision: 4, entries: [] });

      await page().confirmRemove();
      await settle();

      expect(service['replaceNodeConfig']).toHaveBeenCalledWith(4, 3, []);
      expect(page().removeOpen()).toBe(false);
      expect(root.textContent).toContain('Configuration removed');
      expect(root.textContent).toContain('The model stays calibrated');
    });

    it('keeps the dialog open and shows why when the removal is refused', async () => {
      await create();
      page().openRemove();
      service['replaceNodeConfig'].mockRejectedValue({ status: 409, error: { error: 'Configuration changed meanwhile' } });

      await page().confirmRemove();

      expect(page().removeOpen()).toBe(true);
      expect(page().removeError()).toBe('Configuration changed meanwhile');
      expect(page().removing()).toBe(false);
    });

    it('cannot be closed or repeated while it is running', async () => {
      await create();
      let finish: (value: unknown) => void = () => undefined;
      service['replaceNodeConfig'].mockReturnValue(new Promise((resolve) => (finish = resolve)));
      page().openRemove();

      const first = page().confirmRemove();
      page().closeRemove();
      void page().confirmRemove();

      expect(page().removeOpen()).toBe(true);
      expect(service['replaceNodeConfig']).toHaveBeenCalledTimes(1);
      finish({ provider_id: 4, config_revision: 4 });
      await first;
    });
  });

  describe('editing the advanced settings', () => {
    const page = (): NodeModel => fixture.componentInstance;
    const start = async (): Promise<HTMLElement> => {
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      return root;
    };

    it('shows the fields of the entry and counts a change of one of them', async () => {
      const root = await start();

      expect(root.querySelector<HTMLInputElement>('#adv-quantization')!.value).toBe('awq');
      expect(root.querySelector<HTMLInputElement>('#adv-min_context_fraction')!.value).toBe('100');
      expect(page().dirty()).toBe(false);

      page().setAdv('quantization', 'gptq');
      fixture.detectChanges();

      expect(page().dirty()).toBe(true);
      expect(root.textContent).toContain('Quantization: awq → gptq');
    });

    it('blocks Save with a message at the field while a value cannot be right', async () => {
      const root = await start();

      page().setAdv('max_num_seqs', '0');
      fixture.detectChanges();

      expect(root.querySelector('#adv-max_num_seqs')!.getAttribute('aria-invalid')).toBe('true');
      expect(root.textContent).toContain('whole number');
      expect(root.querySelector<HTMLButtonElement>('.savebar .btn-primary')!.disabled).toBe(true);
    });

    it('saves the changed setting with the rest of the entry', async () => {
      const root = await start();
      page().setAdv('max_num_seqs', '16');
      page().addCustom();
      page().setCustom(0, 'key', 'depth');
      page().setCustom(0, 'value', '3');
      fixture.detectChanges();

      root.querySelector<HTMLButtonElement>('.savebar .btn-primary')!.click();
      await settle();

      const [, , entries] = service['replaceNodeConfig'].mock.calls[0];
      expect(entries[0].overrides).toMatchObject({ max_model_len: 32768, max_num_seqs: 16, quantization: 'awq', depth: 3 });
      expect(entries[0].capability.min_context_fraction).toBe(1);
    });

    it('adds and removes custom settings', async () => {
      await start();

      page().addCustom();
      page().addCustom();
      page().removeCustom(0);

      expect(page().adv()!.custom).toHaveLength(1);
    });

    it('shows the worker refusal at a custom setting and applies its suggestion', async () => {
      const root = await start();
      page().addCustom();
      page().setCustom(0, 'key', 'max_modell_len');
      page().setCustom(0, 'value', '8192');
      fixture.detectChanges();
      service['replaceNodeConfig'].mockRejectedValue({
        status: 400,
        error: {
          error: 'org/a: max_modell_len: unknown setting (did you mean max_model_len?)',
          errors: [{ model: 'org/a', key: 'max_modell_len', message: 'unknown setting', suggestion: 'max_model_len' }],
        },
      });

      root.querySelector<HTMLButtonElement>('.savebar .btn-primary')!.click();
      await settle();

      expect(root.textContent).toContain('unknown setting');
      const fix = root.querySelector<HTMLButtonElement>('.fix')!;
      expect(fix.textContent).toContain('max_model_len');
      fix.click();
      fixture.detectChanges();

      expect(page().adv()!.custom[0].key).toBe('max_model_len');
    });

    it('drops the settings draft on cancel and after a save', async () => {
      const root = await start();
      page().setAdv('quantization', 'gptq');

      root.querySelector<HTMLButtonElement>('.savebar .btn-secondary')!.click();
      fixture.detectChanges();
      expect(page().adv()).toBeNull();

      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      expect(page().adv()!.values['quantization']).toBe('awq');
    });
  });
});
