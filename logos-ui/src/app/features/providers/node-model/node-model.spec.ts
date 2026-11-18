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
      calibrateModel: vi.fn().mockResolvedValue({ count: 1, message: 'started' }),
    };
  });

  it('shows the placement and the measured values of the model', async () => {
    const root = await create();

    expect(root.querySelector('h1')?.textContent).toContain('org/a');
    expect(root.textContent).toContain('hochbruegge');
    expect(root.textContent).toContain('TP 4');
    expect(root.textContent).toContain('4 GB');
    expect(root.textContent).toContain('32768');
    expect(root.textContent).toContain('tokens');
    expect(root.textContent).not.toContain('Measured by calibration');
    // Base residency 41.2 GB and 4 GB of KV cache on each of 4 GPUs.
    expect(root.textContent).toContain('56.2 GB');
    expect(root.textContent).toContain('GPU memory');
    expect(root.textContent).toContain('~33k tokens');
    expect(root.textContent).toContain('55 s');
    expect(root.textContent).toContain('wake from sleep 1 s');
    expect(root.querySelectorAll('[role="radio"]').length).toBe(1);
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

    expect(root.textContent).toContain('Not calibrated yet');
    expect(root.textContent).toContain('chosen after calibration');
    expect(root.textContent).not.toContain('Measured by calibration');
    expect(root.querySelector('#cfg-title')).toBeNull();
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
    expect(root.textContent).toContain('Best benchmark at TP 2');
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
    expect(root.textContent).toContain('Configuration');
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

    it('offers no editing while the worker is offline', async () => {
      service['getNodeInsights'].mockResolvedValue(insights([insight()], false));
      expect((await create()).querySelector('.head button')).toBeNull();
    });

    it('offers only the settings, not the placement, of a model that is not calibrated', async () => {
      service['getNodeInsights'].mockResolvedValue(
        insights([insight({ calibration: { status: 'not_calibrated', reason: null, results: [] } })]),
      );
      const root = await create();
      const labels = Array.from(root.querySelectorAll('.head button')).map((b) => b.textContent!.trim());

      expect(labels).toEqual(['Edit settings', 'Calibrate model']);
      expect(root.textContent).not.toContain('Configure model');
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
      expect(root.querySelector('.banner--rec-view button')).toBeNull();

      click(root, '.head button.btn-primary');
      fixture.detectChanges();
      click(root, '.banner--rec button');
      fixture.detectChanges();

      expect(page().editing()).toBe(true);
      expect(page().draft()).toEqual({ tp: 4, kvGb: 2, ctx: 8192 });
    });

    it('says how much faster the benchmark was than the run it was compared with, and links to all benchmarks', async () => {
      const best = {
        benchmark_id: 7,
        throughput: 52.3,
        settings: { kv_cache_memory_bytes: '2G', max_model_len: 8192 },
        recorded_at: null,
        baseline_throughput: 48.4,
      };
      service['getNodeInsights'].mockResolvedValue(
        insights([
          insight({
            model_id: 9,
            benchmark: { best_eligible: best, best_observed: best, current: { kv_cache_memory_bytes: '4G' }, eligible_for_comparison: true },
          }),
        ]),
      );
      const root = await create();

      expect(root.querySelector('.banner--rec-view')!.textContent).toContain('Recommendation not in use');
      expect(root.querySelector('.banner--rec-view')!.textContent).toContain('8 % faster than the current configuration (52.3 vs 48.4 tok/s).');
      expect(root.querySelector('.bm-link')!.getAttribute('href')).toBe('/models/9/details?tab=performance');
    });

    it('names the throughput when no run was compared, and leaves out the link for a model outside the catalog', async () => {
      const best = { benchmark_id: 7, throughput: 52.3, settings: { kv_cache_memory_bytes: '2G' }, recorded_at: null };
      service['getNodeInsights'].mockResolvedValue(
        insights([
          insight({
            benchmark: { best_eligible: best, best_observed: best, current: { kv_cache_memory_bytes: '4G' }, eligible_for_comparison: true },
          }),
        ]),
      );
      const root = await create();

      expect(root.querySelector('.banner--rec-view')!.textContent).toContain('52.3 tok/s, benchmark #7.');
      expect(root.querySelector('.bm-link')).toBeNull();
    });
  });

  describe('answers that arrive late', () => {
    const page = (): NodeModel => fixture.componentInstance;

    it('keeps the newer data when an older request answers last', async () => {
      await create();
      let slow: (value: unknown) => void = () => undefined;
      service['getNodeConfig'].mockReturnValueOnce(new Promise((resolve) => (slow = resolve)));
      const first = page().load();
      service['getNodeConfig'].mockResolvedValueOnce({ ...config, revision: 9 });
      await page().load();

      slow({ ...config, revision: 5 });
      await first;

      expect(page().config()?.revision).toBe(9);
      expect(page().loading()).toBe(false);
    });

    it('does not show the result of a save on the model opened meanwhile', async () => {
      await create();
      page().startEdit();
      let finish: (value: unknown) => void = () => undefined;
      service['replaceNodeConfig'].mockReturnValue(new Promise((resolve) => (finish = resolve)));

      const saving = page().save();
      query.next(convertToParamMap({ model: 'org/b' }));
      await settle();
      finish({ provider_id: 4, config_revision: 4 });
      await saving;
      await settle();

      expect(page().notice()).toBeNull();
      expect(page().saving()).toBe(false);
    });

    it('does not show the failure of a save on the model opened meanwhile', async () => {
      await create();
      page().startEdit();
      let fail: (reason: unknown) => void = () => undefined;
      service['replaceNodeConfig'].mockReturnValue(new Promise((_, reject) => (fail = reject)));

      const saving = page().save();
      query.next(convertToParamMap({ model: 'org/b' }));
      await settle();
      fail({ status: 409, error: { error: 'Configuration changed meanwhile' } });
      await saving;

      expect(page().failure()).toBeNull();
    });

    it('does not say a calibration started on the model opened meanwhile', async () => {
      await create();
      page().modelName.set('org/a');
      let finish: (value: unknown) => void = () => undefined;
      service['calibrateModel'].mockReturnValue(new Promise((resolve) => (finish = resolve)));
      service['getNodeInsights'].mockResolvedValue(
        insights([insight({ calibration: { status: 'not_calibrated', reason: null, results: [] } })]),
      );
      await page().load();

      const starting = page().calibrate();
      query.next(convertToParamMap({ model: 'org/b' }));
      await settle();
      finish({ count: 1, message: 'started' });
      await starting;

      expect(page().notice()).toBeNull();
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
      page().allOpen.set(true);
      fixture.detectChanges();

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

  describe('calibrating the model', () => {
    const uncalibrated = (extra: Partial<NodeModelInsight> = {}) =>
      insights([
        insight({
          row: false,
          configured: false,
          calibration: { status: 'not_calibrated', reason: null, results: [] },
          ...extra,
        }),
      ]);
    const button = (root: HTMLElement): HTMLButtonElement | undefined =>
      Array.from(root.querySelectorAll('button')).find((b) => b.textContent?.includes('Calibrat')) as
        | HTMLButtonElement
        | undefined;

    it('offers the calibration for a model that has none and starts it', async () => {
      service['getNodeInsights'].mockResolvedValue(uncalibrated());
      const root = await create();

      expect(root.textContent).toContain('Calibration settings');
      expect(button(root)!.textContent).toContain('Calibrate model');
      button(root)!.click();
      await settle();

      expect(service['calibrateModel']).toHaveBeenCalledWith(4, 'org/a');
      expect(root.textContent).toContain('Calibration started');
    });

    it('offers it again for an outdated calibration and says why', async () => {
      service['getNodeInsights'].mockResolvedValue(
        insights([insight({ calibration: { status: 'outdated', reason: null, results: [] } })]),
      );
      const root = await create();

      expect(root.textContent).toContain('no longer matches');
      expect(button(root)).toBeDefined();
    });

    it('offers nothing for a calibrated model', async () => {
      const root = await create();

      expect(root.textContent).not.toContain('Calibration settings');
    });

    it('is switched off while the worker is offline or already measuring the model', async () => {
      service['getNodeInsights'].mockResolvedValue({ ...uncalibrated(), worker: { online: false, supports_actions: {}, config_revision: 3 } });
      let root = await create();
      expect(button(root)!.disabled).toBe(true);
      expect(button(root)!.getAttribute('title')).toContain('must be online');

      TestBed.resetTestingModule();
      service['getNodeInsights'].mockResolvedValue(uncalibrated({ calibrating: true }));
      root = await create();
      expect(button(root)!.textContent).toContain('Calibrating');
      expect(button(root)!.disabled).toBe(true);
    });

    it('says so when the worker has nothing to calibrate or refuses', async () => {
      service['getNodeInsights'].mockResolvedValue(uncalibrated());
      const root = await create();
      service['calibrateModel'].mockResolvedValue({ count: 0, message: 'org/a does not need calibration' });

      button(root)!.click();
      await settle();
      expect(root.textContent).toContain('org/a does not need calibration');

      service['calibrateModel'].mockRejectedValue({ status: 409, error: { error: 'Worker is reserved for nightly benchmarks' } });
      button(root)!.click();
      await settle();
      expect(root.textContent).toContain('Worker is reserved for nightly benchmarks');
    });

    it('does not start twice while the request runs', async () => {
      service['getNodeInsights'].mockResolvedValue(uncalibrated());
      const root = await create();
      let finish: (value: unknown) => void = () => undefined;
      service['calibrateModel'].mockReturnValue(new Promise((resolve) => (finish = resolve)));

      button(root)!.click();
      void fixture.componentInstance.calibrate();
      await new Promise((resolve) => setTimeout(resolve, 0));

      expect(service['calibrateModel']).toHaveBeenCalledTimes(1);
      finish({ count: 1 });
      await settle();
    });
  });

  describe('several measured tensor-parallel sizes', () => {
    const twoSizes = () => {
      const base = insight();
      const four = base.calibration.results![0];
      const two = {
        ...four,
        tp: 2,
        active: false,
        kv_min_mb: 2048,
        kv_max_mb: 8192,
        pairs: [
          { kv_mb: 2048, max_model_len: 4096, measured: true },
          { kv_mb: 8192, max_model_len: 65536, measured: true },
        ],
      };
      return insights([{ ...base, calibration: { ...base.calibration, results: [two, four] } }]);
    };

    it('offers every size and starts on the one that is placed', async () => {
      service['getNodeInsights'].mockResolvedValue(twoSizes());
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      expect(root.querySelectorAll('[role="radio"]').length).toBe(2);
      expect(fixture.componentInstance.draft()!.tp).toBe(4);
    });

    it('moves the KV cache and the context into what the other size measured', async () => {
      service['getNodeInsights'].mockResolvedValue(twoSizes());
      await create();
      const page = fixture.componentInstance;
      page.startEdit();

      page.setTp(2);

      expect(page.draft()!.tp).toBe(2);
      expect(page.window()).toEqual({ min: 2, max: 8 });
      expect(page.draft()!.kvGb).toBe(4);
      expect(page.draft()!.ctx).toBeLessThanOrEqual(65536);
      page.setTp(4);
      expect(page.draft()!.tp).toBe(4);
      expect(page.window()).toEqual({ min: 1, max: 4 });
    });

    it('shows the size that is placed when viewing, not the smallest one', async () => {
      service['getNodeInsights'].mockResolvedValue(twoSizes());
      await create();

      expect(fixture.componentInstance.result()!.tp).toBe(4);
    });
  });

  it('says when the nightly tuning set the configuration', async () => {
    service['getNodeInsights'].mockResolvedValue(insights([insight({ source: 'tuning' })]));
    const root = await create();

    expect(root.textContent).toContain('Set by the nightly tuning');
  });

  it('says nothing about the source of a configuration an admin set', async () => {
    const root = await create();

    expect(root.textContent).not.toContain('Set by the nightly tuning');
  });

  describe('the configuration card', () => {
    const gpus = [0, 1, 2, 3].map((index) => ({ index, name: 'A100', total_mb: 40960, used_mb: 100 }));
    const withBenchmark = (settings: Record<string, unknown>) => {
      const best = { benchmark_id: 7, throughput: 52.3, settings, recorded_at: null, ttft_s: 0.47, latency_s: 5.5 };
      return insights([
        insight({
          benchmark: { best_eligible: best, best_observed: best, current: {}, eligible_for_comparison: true },
        }),
      ]);
    };

    it('shows memory use against the GPUs of the size and what is left', async () => {
      service['getNodeInsights'].mockResolvedValue({ ...insights([insight()]), gpus });
      const root = await create();

      // 41.2 GB of weights plus 4 GB of KV cache on each of 4 GPUs of 40 GB.
      expect(root.textContent).toContain('56.2 of 160 GB');
      expect(root.textContent).toContain('103.8 GB free');
      expect(root.textContent).toContain('4 GPUs · 160 GB');
    });

    it('shows the time to first token and the response time of a benchmark that ran this placement', async () => {
      service['getNodeInsights'].mockResolvedValue(
        withBenchmark({ tensor_parallel_size: 4, kv_cache_memory_bytes: '4G', max_model_len: 32768 }),
      );
      const root = await create();

      expect(root.textContent).toContain('52.3 tok/s');
      expect(root.textContent).toContain('0.47 s');
      expect(root.textContent).toContain('5.5 s');
      expect(root.textContent).toContain('Throughput · benchmark');
    });

    it('leaves out the benchmark figures when the benchmark ran another placement', async () => {
      service['getNodeInsights'].mockResolvedValue(
        withBenchmark({ tensor_parallel_size: 2, kv_cache_memory_bytes: '8G', max_model_len: 16384 }),
      );
      const root = await create();

      expect(root.textContent).not.toContain('Throughput · benchmark');
      expect(root.textContent).not.toContain('Time to first token');
    });

    it('draws a scale to the end of the measured window and to the context limit', async () => {
      await create();
      const page = fixture.componentInstance;

      expect(page.kvTicks().map((t) => t.label)).toEqual(['0 GB', '1', '2', '3', '4 GB']);
      expect(page.kvTicks().at(-1)!.pct).toBe(100);
      expect(page.ctxMax()).toBe(32768);
      expect(page.ctxTicks().map((t) => t.label)).toEqual(['0', '32k']);
      expect(page.kvBand()).toEqual({ left: 25, width: 75 });
    });

    it('draws the range in the view without a handle and adds the handle and the input while editing', async () => {
      const root = await create();
      expect(root.querySelectorAll('.sl').length).toBe(2);
      expect(root.querySelectorAll('.sl-win').length).toBe(2);
      expect(root.querySelector('.sl-thumb')).toBeNull();
      expect(root.querySelector('.sl-input')).toBeNull();

      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      expect(root.querySelectorAll('.sl-thumb').length).toBe(2);
      expect(root.querySelectorAll('.sl-input').length).toBe(2);
    });

    it('fills the track from the start of the measured window to the chosen value', async () => {
      await create();
      const page = fixture.componentInstance;

      // The window is 1 to 4 GB on a track of 4 GB, and the chosen value is 4 GB: the fill covers the window.
      expect(page.kvBand()).toEqual({ left: 25, width: 75 });
      expect(page.kvFill()).toBe(75);
    });

    it('marks the benchmark size as best and shows the current configuration while editing', async () => {
      service['getNodeInsights'].mockResolvedValue(withBenchmark({ tensor_parallel_size: 4, kv_cache_memory_bytes: '3G' }));
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      expect(root.querySelector('.best-tag')?.textContent).toContain('Best');
      expect(root.querySelector('.best-chip')?.textContent).toContain('3 GB');
      expect(root.textContent).toContain('Current configuration');
      expect(root.textContent).toContain('4 GB per GPU');
    });

    it('takes the best KV cache size when its chip is selected', async () => {
      service['getNodeInsights'].mockResolvedValue(withBenchmark({ tensor_parallel_size: 4, kv_cache_memory_bytes: '3G' }));
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      root.querySelector<HTMLButtonElement>('.best-chip')!.click();

      expect(fixture.componentInstance.draft()!.kvGb).toBe(3);
    });
  });

  describe('the advanced settings in groups', () => {
    const gpus = [0, 1].map((index) => ({ index, name: 'A100', total_mb: 40960, used_mb: 10240 }));
    const open = async (): Promise<HTMLElement> => {
      service['getNodeInsights'].mockResolvedValue({ ...insights([insight()]), gpus });
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      return root;
    };
    const page = (): NodeModel => fixture.componentInstance;

    it('shows placement and engine at once and keeps the rest under All engine settings', async () => {
      const root = await open();

      expect(Array.from(root.querySelectorAll('h3.group')).map((h) => h.textContent!.trim())).toEqual(['Placement', 'Engine']);
      expect(root.querySelector('#adv-dtype')).toBeNull();

      Array.from(root.querySelectorAll('button')).find((b) => b.textContent?.includes('All engine settings'))!.click();
      fixture.detectChanges();

      expect(root.querySelector('#adv-dtype')).not.toBeNull();
      expect(Array.from(root.querySelectorAll('h3.group')).map((h) => h.textContent!.trim())).toContain('Memory and precision');
      expect(root.textContent).toContain('can mark the calibration of this model as outdated');
    });

    it('narrows the list to what the filter matches and opens the lower part for it', async () => {
      const root = await open();

      page().filter.set('quant');
      fixture.detectChanges();

      expect(root.querySelector('#adv-quantization')).not.toBeNull();
      expect(root.querySelector('#adv-max_num_seqs')).toBeNull();
      page().filter.set('kv_cache_dtype');
      fixture.detectChanges();
      expect(root.querySelector('#adv-kv_cache_dtype')).not.toBeNull();
      page().filter.set('nothing matches this');
      fixture.detectChanges();
      expect(root.querySelectorAll('.row').length).toBe(0);
    });

    it('marks a set value and clears it with Use default', async () => {
      const root = await open();
      const before = page().setCount();

      expect(root.querySelectorAll('.set-dot').length).toBeGreaterThan(0);
      expect(page().isSet('min_context_fraction')).toBe(true);
      (Array.from(root.querySelectorAll('.row button.link')).find((b) =>
        b.closest('.row')!.textContent!.includes('Minimum context share'),
      ) as HTMLButtonElement).click();
      fixture.detectChanges();

      expect(page().isSet('min_context_fraction')).toBe(false);
      expect(page().advChanges().some((c) => c.includes('Minimum context share'))).toBe(true);
      expect(page().setCount()).toBe(before - 1);
    });

    it('hides the reset link of a row without a value instead of removing it', async () => {
      const root = await open();
      const rows = Array.from(root.querySelectorAll('.row')).filter((r) => r.querySelector('button.link--reset'));

      expect(rows.length).toBeGreaterThan(2);
      const hidden = rows.filter((r) => r.querySelector('button.link--hidden'));
      const shown = rows.filter((r) => !r.querySelector('button.link--hidden'));
      expect(hidden.length).toBeGreaterThan(0);
      expect(shown.length).toBeGreaterThan(0);
      // The stylesheet is not applied in the test: `visibility: hidden` and the corner place of a wide row are
      // checked in the browser. Here the link must exist in every row and carry the class that hides it.
      expect(rows.every((r) => r.querySelector('button.link--reset'))).toBe(true);
    });

    it('chooses the GPUs of the model with tiles and clears the setting for all of them', async () => {
      const root = await open();
      expect(root.querySelectorAll('.gpus--compact .gpu--on').length).toBe(2);
      expect(root.textContent).toContain('30.0 GB free');

      page().toggleModelGpu(1);
      fixture.detectChanges();
      expect(page().adv()!.values['gpu_devices']).toBe('0');
      expect(root.querySelectorAll('.gpus--compact .gpu--on').length).toBe(1);

      page().toggleModelGpu(0);
      expect(page().adv()!.values['gpu_devices']).toBe('0');

      page().toggleModelGpu(1);
      expect(page().adv()!.values['gpu_devices']).toBe('');
    });

    it('shows the recommendation as a banner of the editor and applies it', async () => {
      const best = { benchmark_id: 7, throughput: 52.3, settings: { tensor_parallel_size: 4, kv_cache_memory_bytes: '2G', max_model_len: 8192 }, recorded_at: null };
      service['getNodeInsights'].mockResolvedValue({
        ...insights([
          insight({ benchmark: { best_eligible: best, best_observed: best, current: { tensor_parallel_size: 4, max_model_len: 32768 }, eligible_for_comparison: true } }),
        ]),
        gpus,
      });
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      expect(root.querySelector('.banner--rec')!.textContent).toContain('Recommended by benchmark');
      root.querySelector<HTMLButtonElement>('.banner--rec button')!.click();
      fixture.detectChanges();

      expect(page().draft()).toEqual({ tp: 4, kvGb: 2, ctx: 8192 });
    });
  });

  describe('the current configuration while editing', () => {
    const side = (root: HTMLElement): string => root.querySelector('[aria-labelledby="cur-title"]')!.textContent!;

    it('keeps showing what is stored while values change and shows the new values after a save', async () => {
      service['getNodeInsights'].mockResolvedValue({ ...insights([insight()]), gpus: [{ index: 0, name: 'A', total_mb: 40960, used_mb: 0 }] });
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      const before = side(root);
      expect(before).toContain('4 GB per GPU');
      expect(before).toContain('32,768 tokens');

      fixture.componentInstance.setKv(2);
      fixture.componentInstance.setCtx(8192);
      fixture.detectChanges();

      expect(side(root)).toBe(before);
      expect(fixture.componentInstance.draft()).toEqual({ tp: 4, kvGb: 2, ctx: 8192 });
      // The tiles under the sliders follow the draft.
      expect(root.querySelector('.tiles')!.textContent).not.toBe('');
      expect(fixture.componentInstance.figures()).not.toEqual(fixture.componentInstance.savedFigures());
    });

    it('is not shown for a model that has no stored placement yet', async () => {
      service['getNodeInsights'].mockResolvedValue(insights([insight({ row: false, configured: false })]));
      service['getNodeConfig'].mockResolvedValue({ ...config, entries: [] });
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();

      expect(root.querySelector('[aria-labelledby="cur-title"]')).toBeNull();
    });
  });

  describe('what happens around a save', () => {
    const page = (): NodeModel => fixture.componentInstance;

    it('does not call a save failed when only the reload afterwards failed', async () => {
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      page().setAdv('max_num_seqs', '16');
      service['getNodeConfig'].mockRejectedValue({ status: 502, error: { error: 'bad gateway' } });

      await page().save();
      fixture.detectChanges();

      expect(service['replaceNodeConfig']).toHaveBeenCalledTimes(1);
      expect(page().failure()).toBeNull();
      expect(page().editing()).toBe(false);
      expect(page().notice()!.title).toBe('Saved');
      expect(page().notice()!.text).toContain('could not load them again');
    });

    it('does not call a started calibration failed when the status cannot be read again', async () => {
      service['getNodeInsights'].mockResolvedValue(
        insights([insight({ calibration: { status: 'not_calibrated', reason: null, results: [] } })]),
      );
      await create();
      service['getNodeInsights'].mockRejectedValue(new Error('down'));

      await page().calibrate();

      expect(page().calibrationError()).toBeNull();
      expect(page().notice()!.title).toBe('Calibration started');
    });

    it('drops the editor, its notices and its errors when another model is opened', async () => {
      const root = await create();
      root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
      fixture.detectChanges();
      page().setKv(2);
      page().setAdv('quantization', 'gptq');
      expect(page().editing()).toBe(true);

      query.next(convertToParamMap({ model: 'org/b' }));
      await settle();

      expect(page().modelName()).toBe('org/b');
      expect(page().editing()).toBe(false);
      expect(page().draft()).toBeNull();
      expect(page().adv()).toBeNull();
      expect(page().notice()).toBeNull();
    });

    it('keeps the answer of the newest load when an earlier one arrives late', async () => {
      await create();
      let late: (value: unknown) => void = () => undefined;
      service['getNodeInsights'].mockReturnValueOnce(new Promise((resolve) => (late = resolve)));
      service['getNodeInsights'].mockResolvedValueOnce(insights([insight({ model_name: 'org/b' })]));

      query.next(convertToParamMap({ model: 'org/b' }));
      query.next(convertToParamMap({ model: 'org/b', again: '1' }));
      await settle();
      late(insights([insight({ model_name: 'org/old' })]));
      await settle();

      expect(page().insights()!.models[0].model_name).toBe('org/b');
      expect(page().loading()).toBe(false);
    });
  });

  it('keeps a right column while editing even for a model without a benchmark', async () => {
    const root = await create();
    expect(root.querySelector('.grid--single')).not.toBeNull();

    root.querySelector<HTMLElement>('.head button.btn-primary')!.click();
    fixture.detectChanges();

    expect(root.querySelector('.grid--single')).toBeNull();
    expect(root.querySelector('[aria-labelledby="cur-title"]')).not.toBeNull();
  });
});
