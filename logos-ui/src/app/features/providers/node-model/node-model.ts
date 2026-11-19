import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  OnInit,
  signal,
  viewChild,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { ModalConfirmComponent } from '../../../shared/components/modal/modal-confirm/modal-confirm';
import { combineLatest } from 'rxjs';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { CalibrationResult, NodeConfig, NodeGpuInsight, NodeInsights } from '../../../shared/models/node-config.model';
import { CalibrationLabel, calibrationLabel, hasTuning } from '../node-models/node-model-status';
import {
  ADVANCED_FIELDS,
  AdvancedDraft,
  AdvancedField,
  FIELD_GROUPS,
  FieldGroup,
  advancedChanges,
  advancedErrors,
  initialAdvanced,
} from './node-model-advanced';
import {
  contextCapacity,
  ErrorItem,
  kvWindowGb,
  loadFailure,
  Placement,
  saveFailure,
  SaveFailure,
  withPlacement,
  withSettings,
} from './node-model-edit';
import {
  advancedSettings,
  describePlacement,
  entrySettings,
  formatGb,
  formatSeconds,
  formatTokens,
  kvToGb,
  mbToGb,
} from './node-model-format';

/** How often the page reads the state again while a calibration runs. It ends without an event for this page. */
export const CALIBRATION_POLL_MS = 10000;
const CONTEXT_MIN = 1024;

/** One model on one logosnode: its configuration and what Logos measured. */
@Component({
  selector: 'app-node-model',
  standalone: true,
  imports: [ErrorMessageComponent, ModalConfirmComponent, NgTemplateOutlet, RouterLink],
  templateUrl: './node-model.html',
  styleUrls: ['../node-page.scss', './node-model.scss'],
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeModel implements OnInit {
  private readonly service = inject(ProviderManagementService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly providerId = signal<number | null>(null);
  readonly modelName = signal('');
  readonly providerName = signal('');
  readonly config = signal<NodeConfig | null>(null);
  readonly insights = signal<NodeInsights | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly insight = computed(() => this.insights()?.models.find((m) => m.model_name === this.modelName()) ?? null);
  readonly entry = computed(() => this.config()?.entries.find((e) => e.model_name === this.modelName()));
  readonly workerOnline = computed(() => this.insights()?.worker.online ?? false);
  readonly settings = computed(() => entrySettings(this.entry()));
  readonly advanced = computed(() => advancedSettings(this.entry()));
  readonly label = computed<CalibrationLabel | null>(() => {
    const insight = this.insight();
    return insight ? calibrationLabel(insight) : null;
  });
  readonly tuning = computed(() => {
    const insight = this.insight();
    return insight !== null && hasTuning(insight);
  });
  readonly recommendation = computed(() => this.insight()?.benchmark?.best_eligible ?? null);
  /** The measured result of the tensor-parallel size being edited or placed, else the one the lane uses. */
  readonly result = computed(() => {
    const results = this.insight()?.calibration.results ?? [];
    const tp = this.draft()?.tp ?? this.saved()?.tp;
    return results.find((r) => r.tp === tp) ?? results.find((r) => r.active) ?? results[0] ?? null;
  });

  readonly kvGb = computed(() => kvToGb(this.settings()['kv_cache_memory_bytes']));

  /** What the page shows: the draft while editing, else the stored placement. */
  readonly placement = computed<Placement | null>(() => this.draft() ?? this.saved());
  /** The best benchmark run, eligible or not. */
  readonly bestRun = computed(() => {
    const b = this.insight()?.benchmark;
    return b ? (b.best_eligible ?? b.best_observed) : null;
  });
  readonly bestTp = computed(() => Number(this.recommendation()?.settings['tensor_parallel_size']) || null);
  readonly bestKv = computed(() => kvToGb(this.recommendation()?.settings['kv_cache_memory_bytes']));
  readonly bestCtx = computed(() => Number(this.recommendation()?.settings['max_model_len']) || null);
  /** The measured sizes while editing, the placed one while viewing. */
  readonly tpTiles = computed(() => {
    const all = this.insight()?.calibration.results ?? [];
    if (this.editing()) {
      return all;
    }
    const tp = this.placement()?.tp;
    const placed = all.filter((r) => r.tp === tp);
    return placed.length > 0 ? placed : all.filter((r) => r.active);
  });
  /** The largest context the sliders show: the limit of the model, else the best the sweep reached. */
  readonly ctxMax = computed(() => {
    const limit = this.insight()?.calibration.context_limit;
    const sweep = Math.max(0, ...(this.result()?.pairs.map((p) => p.max_model_len ?? 0) ?? [0]));
    return Math.max(1024, limit ?? sweep);
  });
  readonly kvTicks = computed(() => {
    const w = this.window();
    if (!w) {
      return [];
    }
    const step = w.max <= 6 ? 1 : 2;
    const ticks: { label: string; pct: number }[] = [];
    for (let v = 0; v < w.max; v += step) {
      if (v === 0 || (w.max - v) / w.max > 0.08) {
        ticks.push({ label: v === 0 ? '0 GB' : String(v), pct: (v / w.max) * 100 });
      }
    }
    ticks.push({ label: `${w.max} GB`, pct: 100 });
    return ticks;
  });
  /** The smallest context the sliders and the field take; the scale starts here, not at 0. */
  readonly ctxMin = CONTEXT_MIN;
  /** Where a context length lies on the slider, in percent of the track. */
  ctxPct(tokens: number): number {
    const span = Math.max(1, this.ctxMax() - CONTEXT_MIN);
    return Math.min(100, Math.max(0, ((tokens - CONTEXT_MIN) / span) * 100));
  }
  readonly ctxTicks = computed(() => {
    const max = this.ctxMax();
    const ticks: { label: string; pct: number }[] = [{ label: `${CONTEXT_MIN / 1024}k`, pct: 0 }];
    for (let v = 32768; v < max * 0.92; v += 32768) {
      ticks.push({ label: `${v / 1024}k`, pct: this.ctxPct(v) });
    }
    ticks.push({ label: `${Math.round(max / 1024)}k`, pct: 100 });
    return ticks;
  });
  /** How far the filled part of the KV cache slider reaches, in percent of the track. */
  readonly kvFill = computed(() => {
    const w = this.window();
    const p = this.placement();
    const band = this.kvBand();
    return w && p && band ? Math.max(0, (p.kvGb / w.max) * 100 - band.left) : 0;
  });
  /** Where the measured window lies on the KV cache slider, in percent. */
  readonly kvBand = computed(() => {
    const w = this.window();
    return w ? { left: (w.min / w.max) * 100, width: ((w.max - w.min) / w.max) * 100 } : null;
  });
  /** How far the context can go at the chosen KV cache size, in percent of the slider. */
  readonly ctxBand = computed(() => {
    const cap = this.capacity() ?? this.capacityAtPlacement();
    return cap ? this.ctxPct(cap) : 100;
  });
  readonly capacityAtPlacement = computed(() => {
    const p = this.placement();
    const r = this.result();
    return p && r ? contextCapacity(r.pairs, p.kvGb, this.insight()?.calibration.context_limit) : null;
  });
  /** Figures that follow from the placement being edited or shown. */
  readonly figures = computed(() => this.figuresFor(this.placement()));
  /** The same figures for what is stored: the side card shows these and changes only after a save. */
  readonly savedFigures = computed(() => this.figuresFor(this.saved()));

  /**
   * The memory a placement takes. The measured total already holds the cache the calibration gave vLLM,
   * so that part is replaced by the chosen cache, not added to. Without it the measured total stands.
   */
  private usedMemoryMb(r: CalibrationResult, p: Placement): number | null {
    if (r.base_residency_mb == null) {
      return null;
    }
    const measured = (r.measured_kv_mb ?? 0) * r.tp;
    return measured > 0 && measured < r.base_residency_mb
      ? r.base_residency_mb - measured + p.kvGb * 1024 * p.tp
      : r.base_residency_mb;
  }

  private figuresFor(p: Placement | null): { value: string; caption: string }[] {
    const results = this.insight()?.calibration.results ?? [];
    const r = results.find((x) => x.tp === p?.tp) ?? this.result();
    if (!p || !r) {
      return [];
    }
    const gb = (mb: number) => (mb / 1024).toFixed(1);
    const out: { value: string; caption: string }[] = [];
    const usedMb = this.usedMemoryMb(r, p);
    const gpuMb = this.insights()?.gpus.find((g) => g.total_mb)?.total_mb ?? null;
    if (usedMb !== null && gpuMb) {
      out.push({
        value: `${gb(usedMb)} of ${Math.round((gpuMb * p.tp) / 1024)} GB`,
        caption: `GPU memory · ${gb(gpuMb * p.tp - usedMb)} GB free`,
      });
    } else if (usedMb !== null) {
      out.push({ value: `${gb(usedMb)} GB`, caption: 'GPU memory' });
    }
    const cap = contextCapacity(r.pairs, p.kvGb, this.insight()?.calibration.context_limit);
    if (cap) {
      out.push({ value: `~${cap >= 1000 ? Math.round(cap / 1000) + 'k' : cap} tokens`, caption: 'Context capacity per request' });
    }
    const run = this.bestRun();
    if (run && this.matches(run.settings, p)) {
      if (run.ttft_s) {
        out.push({ value: `${run.ttft_s.toFixed(2)} s`, caption: 'Time to first token · benchmark' });
      }
      if (run.latency_s) {
        out.push({ value: `${run.latency_s.toFixed(1)} s`, caption: 'Response time · benchmark' });
      }
    }
    if (r.cold_load_time_s != null) {
      const wake = r.wake_from_sleep_time_s != null ? ` · wake from sleep ${Math.round(r.wake_from_sleep_time_s)} s` : '';
      out.push({ value: `${Math.round(r.cold_load_time_s)} s`, caption: `Cold start${wake}` });
    }
    return out;
  }

  /** "8 % faster than the current configuration (52.3 vs 48.4 tok/s)", when the run had a baseline to compare with. */
  readonly speedup = computed(() => {
    const run = this.recommendation();
    const base = run?.baseline_throughput;
    if (!run || !base || base <= 0 || run.throughput <= base) {
      return null;
    }
    const pct = Math.round((run.throughput / base - 1) * 100);
    return `${pct} % faster than the current configuration (${run.throughput.toFixed(1)} vs ${base.toFixed(1)} tok/s).`;
  });

  /** The tensor-parallel size of the best benchmark run. */
  readonly bestRunTp = computed(() => Number(this.bestRun()?.settings['tensor_parallel_size']) || null);

  /** Memory of the GPUs a size uses, in GB; 0 when the worker reports no GPU. */
  gpuSizeGb(tp: number): number {
    const mb = this.insights()?.gpus.find((g) => g.total_mb)?.total_mb ?? 0;
    return Math.round((mb * tp) / 1024);
  }

  /** Whether the serving settings of a benchmark run are this placement. */
  private matches(settings: Record<string, unknown>, p: Placement): boolean {
    const tp = Number(settings['tensor_parallel_size']);
    const kv = kvToGb(settings['kv_cache_memory_bytes']);
    const ctx = Number(settings['max_model_len']);
    // Sizes come from text like "8G" and from a slider; compare to the precision either shows.
    return tp === p.tp && kv !== null && Math.abs(kv - p.kvGb) < 0.05 && (!ctx || ctx === p.ctx);
  }

  // Editing
  readonly editing = signal(false);
  /** The revision of the configuration the open editor started from; the save is checked against it. */
  private editRevision: number | null = null;
  readonly draft = signal<Placement | null>(null);
  readonly adv = signal<AdvancedDraft | null>(null);
  private readonly advStart = signal<AdvancedDraft | null>(null);
  readonly fields = ADVANCED_FIELDS;
  readonly groups = FIELD_GROUPS;
  readonly filter = signal('');
  readonly allOpen = signal(false);
  /** The engine settings below the first two groups show when asked for or when a filter is typed. */
  readonly showAll = computed(() => this.allOpen() || this.filter().trim() !== '');
  /** How many settings the draft sets: the fields with a value and the custom rows. */
  readonly setCount = computed(() => {
    const a = this.adv();
    return a ? Object.values(a.values).filter((v) => v.trim() !== '').length + a.custom.filter((r) => r.key.trim() !== '').length : 0;
  });
  readonly saving = signal(false);
  readonly failure = signal<SaveFailure | null>(null);
  readonly notice = signal<{ title: string; text: string } | null>(null);
  readonly calibrationStarting = signal(false);
  /** The question "Calibrate this model?" is open. */
  readonly calibrateAsk = signal(false);
  /** The question "Stop the calibration?" is open. */
  readonly stopAsk = signal(false);
  readonly calibrationStopping = signal(false);
  readonly calibrationError = signal<string | null>(null);
  /** Which request the error belongs to: the start or the stop of a calibration. */
  readonly calibrationErrorOf = signal<'start' | 'stop'>('start');
  /** A model that is not calibrated or whose calibration is outdated can be measured now. */
  readonly canCalibrate = computed(() => {
    const status = this.insight()?.calibration.status;
    return status === 'not_calibrated' || status === 'outdated';
  });
  readonly calibrating = computed(() => this.insight()?.calibrating ?? false);
  readonly outdated = computed(() => this.insight()?.calibration.status === 'outdated');
  private readonly failureBanner = viewChild<ElementRef<HTMLElement>>('failureBanner');

  /** A calibrated model can be placed while its worker can check the list. */
  readonly canEdit = computed(() => this.workerOnline() && this.result() !== null);
  /** The settings of a model can be edited before it has a calibration: only its placement needs one. */
  readonly canEditSettings = computed(() => this.workerOnline() && this.insight() !== null);
  /** A model with no measurement has no placement to show. */
  readonly measured = computed(() => this.result() !== null);
  readonly window = computed(() => kvWindowGb(this.result()));
  readonly saved = computed<Placement | null>(() => {
    const s = this.settings();
    const kv = this.kvGb();
    const tp = Number(s['tensor_parallel_size']);
    const ctx = Number(s['max_model_len']);
    return this.insight()?.configured && kv !== null && tp > 0 && ctx > 0 ? { tp, kvGb: kv, ctx } : null;
  });
  readonly capacity = computed(() => {
    const d = this.draft();
    const r = this.result();
    return d && r ? contextCapacity(r.pairs, d.kvGb, this.insight()?.calibration.context_limit) : null;
  });
  readonly advChanges = computed(() => {
    const start = this.advStart();
    const now = this.adv();
    return start && now ? advancedChanges(start, now) : [];
  });
  /** Problems the editor finds itself, by field key or "custom:<row>". */
  readonly advErrors = computed(() => {
    const now = this.adv();
    return now ? advancedErrors(now) : {};
  });
  readonly hasAdvErrors = computed(() => Object.keys(this.advErrors()).length > 0);
  readonly dirty = computed(() => {
    const d = this.draft();
    const s = this.saved();
    const placed = d !== null && (s === null || s.tp !== d.tp || s.kvGb !== d.kvGb || s.ctx !== d.ctx);
    return placed || this.advChanges().length > 0;
  });
  readonly changes = computed(() => {
    const d = this.draft();
    const s = this.saved();
    if (!d) return this.advChanges();
    const out: string[] = [];
    if (!s) {
      out.push(`TP ${d.tp}`, `KV cache ${d.kvGb} GB per GPU`, `context ${d.ctx}`);
    } else {
      if (s.tp !== d.tp) out.push(`TP ${s.tp} → ${d.tp}`);
      if (s.kvGb !== d.kvGb) out.push(`KV cache ${s.kvGb} → ${d.kvGb} GB per GPU`);
      if (s.ctx !== d.ctx) out.push(`Context ${s.ctx} → ${d.ctx}`);
    }
    return [...out, ...this.advChanges()];
  });

  readonly formatGb = formatGb;
  readonly formatSeconds = formatSeconds;
  readonly formatTokens = formatTokens;
  readonly describePlacement = describePlacement;
  readonly mbToGb = mbToGb;

  constructor() {
    effect((onCleanup) => {
      if (!this.calibrating()) {
        return;
      }
      // A change the admin is typing, or saving, must not meet a configuration that moved under it.
      const timer = setInterval(() => {
        if (!this.editing() && !this.saving()) {
          void this.tryRefresh();
        }
      }, CALIBRATION_POLL_MS);
      onCleanup(() => clearInterval(timer));
    });
  }

  ngOnInit(): void {
    combineLatest([this.route.paramMap, this.route.queryParamMap])
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(([params, query]) => {
        const id = Number(params.get('providerId'));
        const model = query.get('model') ?? '';
        if (id !== this.providerId() || model !== this.modelName()) {
          // Another model: nothing of the editor, its notices or its errors belongs to it.
          this.resetEditing();
          this.notice.set(null);
          this.failure.set(null);
          this.calibrationError.set(null);
          // What still runs for the model left belongs to it; it must not keep this page busy.
          this.saving.set(false);
          this.calibrationStarting.set(false);
        }
        this.providerId.set(id);
        this.modelName.set(model);
        void this.load();
      });
  }

  /** The latest request for the page data; an answer to an older one is dropped. */
  private loadToken = 0;

  /** The model this page shows; an action that finishes for another one leaves the page alone. */
  private pageKey(): string {
    return `${this.providerId()}:${this.modelName()}`;
  }

  private resetEditing(): void {
    this.editing.set(false);
    this.editRevision = null;
    this.draft.set(null);
    this.adv.set(null);
    this.advStart.set(null);
    this.filter.set('');
    this.allOpen.set(false);
  }

  async load(): Promise<void> {
    const token = ++this.loadToken;
    const id = this.providerId();
    if (id === null || Number.isNaN(id) || !this.modelName()) {
      this.loading.set(false);
      this.error.set('This page needs a node and a model.');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    try {
      const [config, insights, providers] = await Promise.all([
        this.service.getNodeConfig(id),
        this.service.getNodeInsights(id),
        this.service.getProviders(),
      ]);
      if (token !== this.loadToken) {
        return;
      }
      this.config.set(config);
      this.insights.set(insights);
      this.providerName.set(providers.find((p) => p.id === id)?.name ?? `Node ${id}`);
    } catch (e) {
      if (token === this.loadToken) {
        this.error.set(loadFailure(e, 'This model could not be loaded.'));
      }
    } finally {
      if (token === this.loadToken) {
        this.loading.set(false);
      }
    }
  }

  /** Start: what is stored, else the best benchmark, else the measured top. */
  private initialPlacement(): Placement | null {
    const result = this.result();
    const window = this.window();
    if (!result || !window) {
      return null;
    }
    const stored = this.saved();
    if (stored) {
      return stored;
    }
    const best = this.recommendation()?.settings ?? {};
    const tp = Number(best['tensor_parallel_size']) || result.tp;
    const kv = kvToGb(best['kv_cache_memory_bytes']) ?? window.max;
    const kvGb = Math.min(window.max, Math.max(window.min, Math.round(kv)));
    const cap = contextCapacity(result.pairs, kvGb, this.insight()?.calibration.context_limit);
    const ctx = Number(best['max_model_len']) || cap || 1024;
    return { tp, kvGb, ctx: cap ? Math.min(ctx, cap) : ctx };
  }

  startEdit(): void {
    if (!this.canEditSettings()) {
      return;
    }
    // A model without a calibration has no placement: only its settings are edited.
    this.draft.set(this.initialPlacement());
    const settings = initialAdvanced(this.entry());
    this.adv.set(settings);
    this.advStart.set(structuredClone(settings));
    this.failure.set(null);
    this.notice.set(null);
    // A read of the page data that is still on its way must not replace what this draft starts from.
    this.loadToken++;
    this.editRevision = this.config()?.revision ?? null;
    this.editing.set(true);
  }

  cancelEdit(): void {
    if (this.saving()) {
      return;
    }
    this.editing.set(false);
    this.editRevision = null;
    this.draft.set(null);
    this.adv.set(null);
    this.failure.set(null);
  }

  /** Switch to another measured size: the KV cache and context move into what that size measured. */
  setTp(tp: number): void {
    const d = this.draft();
    const target = this.insight()?.calibration.results.find((r) => r.tp === tp);
    if (!d || !target || d.tp === tp) {
      return;
    }
    const window = kvWindowGb(target);
    const kvGb = window ? Math.min(window.max, Math.max(window.min, d.kvGb)) : d.kvGb;
    const cap = contextCapacity(target.pairs, kvGb, this.insight()?.calibration.context_limit);
    this.draft.set({ tp, kvGb, ctx: cap ? Math.min(d.ctx, cap) : d.ctx });
  }

  setKv(value: unknown): void {
    const d = this.draft();
    const window = this.window();
    const n = Math.round(Number(value));
    if (!d || !window || !Number.isFinite(n)) {
      return;
    }
    const kvGb = Math.min(window.max, Math.max(window.min, n));
    const cap = contextCapacity(this.result()!.pairs, kvGb, this.insight()?.calibration.context_limit);
    this.draft.set({ ...d, kvGb, ctx: cap ? Math.min(d.ctx, cap) : d.ctx });
  }

  setCtx(value: unknown): void {
    const d = this.draft();
    const n = Math.round(Number(value) / CONTEXT_MIN) * CONTEXT_MIN;
    if (!d || !Number.isFinite(n)) {
      return;
    }
    const cap = this.capacity();
    this.draft.set({ ...d, ctx: Math.max(CONTEXT_MIN, cap ? Math.min(n, cap) : n) });
  }

  /** Whether the placement being edited is the one the benchmark recommends, in every value the run recorded. */
  readonly recommendationApplied = computed(() => {
    const d = this.draft();
    const best = this.recommendation()?.settings ?? {};
    if (!d) {
      return false;
    }
    const tp = Number(best['tensor_parallel_size']) || null;
    const kv = kvToGb(best['kv_cache_memory_bytes']);
    const ctx = Number(best['max_model_len']) || null;
    return (
      (tp === null || tp === d.tp) &&
      (kv === null || Math.abs(kv - d.kvGb) < 0.05) &&
      (ctx === null || ctx === d.ctx)
    );
  });

  applyRecommendation(): void {
    if (!this.editing()) {
      this.startEdit();
    }
    const d = this.draft();
    const best = this.recommendation()?.settings ?? {};
    if (!d) {
      return;
    }
    const tp = Number(best['tensor_parallel_size']);
    if (tp && this.insight()?.calibration.results.some((r) => r.tp === tp)) {
      this.setTp(tp);
    }
    if (!this.window()) {
      return;
    }
    this.setKv(kvToGb(best['kv_cache_memory_bytes']) ?? this.draft()!.kvGb);
    this.setCtx(Number(best['max_model_len']) || this.draft()!.ctx);
  }

  /** The fields of a group that match the filter. */
  fieldsOf(group: FieldGroup): AdvancedField[] {
    const q = this.filter().trim().toLowerCase();
    return this.fields.filter(
      (f) => f.group === group.id && (q === '' || f.label.toLowerCase().includes(q) || f.key.includes(q)),
    );
  }

  /** Whether a group of the lower part has a field left after filtering. */
  groupVisible(group: FieldGroup): boolean {
    return (group.top || this.showAll()) && this.fieldsOf(group).length > 0;
  }

  isSet(key: string): boolean {
    return (this.adv()?.values[key] ?? '').trim() !== '';
  }

  /** The GPUs of this node; empty when the worker reports none. */
  nodeGpus(): NodeGpuInsight[] {
    return this.insights()?.gpus ?? [];
  }

  /** A GPU is chosen when the list names it; no list means all of them. */
  modelGpuOn(index: number): boolean {
    const text = (this.adv()?.values['gpu_devices'] ?? '').trim();
    return text === '' || text.split(',').map(Number).includes(index);
  }

  /** A tile adds or drops one GPU; all GPUs clear the setting, and the last GPU cannot be dropped. */
  toggleModelGpu(index: number): void {
    const all = this.nodeGpus().map((g) => g.index);
    const chosen = all.filter((g) => (g === index ? !this.modelGpuOn(g) : this.modelGpuOn(g)));
    if (chosen.length === 0) {
      return;
    }
    this.setAdv('gpu_devices', chosen.length === all.length ? '' : chosen.join(','));
  }

  gpuFreeGb(gpu: NodeGpuInsight): string {
    return gpu.total_mb ? `${(((gpu.total_mb - (gpu.used_mb ?? 0)) / 1024)).toFixed(1)} GB free` : '';
  }

  gpuUsedPct(gpu: NodeGpuInsight): number {
    return gpu.total_mb ? Math.min(100, ((gpu.used_mb ?? 0) / gpu.total_mb) * 100) : 0;
  }

  setAdv(key: string, value: string): void {
    const now = this.adv();
    if (now) this.adv.set({ ...now, values: { ...now.values, [key]: value } });
  }

  addCustom(): void {
    const now = this.adv();
    if (now) this.adv.set({ ...now, custom: [...now.custom, { key: '', value: '' }] });
  }

  removeCustom(index: number): void {
    const now = this.adv();
    if (now) this.adv.set({ ...now, custom: now.custom.filter((_, i) => i !== index) });
  }

  setCustom(index: number, part: 'key' | 'value', value: string): void {
    const now = this.adv();
    if (now) {
      this.adv.set({ ...now, custom: now.custom.map((row, i) => (i === index ? { ...row, [part]: value } : row)) });
    }
  }

  /** What is wrong with a field: our own check first, else the worker's. */
  advFieldError(key: string): string | null {
    return this.advErrors()[key] ?? this.fieldError(key)?.message ?? null;
  }

  customError(index: number): string | null {
    const row = this.adv()?.custom[index];
    return this.advErrors()[`custom:${index}`] ?? (row ? this.fieldError(row.key.trim())?.message ?? null : null);
  }

  /** The key the worker suggests for a custom setting it did not know. */
  customSuggestion(index: number): string | null {
    const row = this.adv()?.custom[index];
    return (row ? this.fieldError(row.key.trim())?.suggestion : null) ?? null;
  }

  /** The message of the worker about one field, if it refused it. */
  fieldError(key: string): ErrorItem | null {
    return this.failure()?.items.find((i) => i.key === key) ?? null;
  }

  async save(): Promise<void> {
    const id = this.providerId();
    const cfg = this.config();
    const d = this.draft();
    const adv = this.adv();
    if (id === null || !cfg || (!d && !adv) || this.saving()) {
      return;
    }
    const page = this.pageKey();
    this.saving.set(true);
    this.failure.set(null);
    try {
      await this.service.replaceNodeConfig(
        id,
        this.editRevision ?? cfg.revision,
        d ? withPlacement(cfg.entries, this.modelName(), d, adv ?? undefined) : withSettings(cfg.entries, this.modelName(), adv!),
      );
    } catch (e) {
      if (page !== this.pageKey()) {
        return;
      }
      this.failure.set(saveFailure(e));
      queueMicrotask(() => this.failureBanner()?.nativeElement.focus());
      this.saving.set(false);
      return;
    }
    if (page !== this.pageKey()) {
      return;
    }
    // The change is stored. Whether the page can show it now is a separate question.
    this.editing.set(false);
    this.editRevision = null;
    this.draft.set(null);
    this.adv.set(null);
    const reloaded = await this.tryRefresh();
    if (page !== this.pageKey()) {
      return;
    }
    this.notice.set({
      title: 'Saved',
      text: reloaded
        ? 'The worker applies the new settings from the next lane start.'
        : 'The settings are saved, but this page could not load them again. Reload the page to see them.',
    });
    this.saving.set(false);
  }

  /** Loads the page data again; false when that failed, which never undoes what was stored before. */
  private async tryRefresh(): Promise<boolean> {
    try {
      await this.refresh();
      return true;
    } catch {
      return false;
    }
  }

  /** Stops the calibration of the node. The admin confirmed first. */
  async stopCalibration(): Promise<void> {
    this.stopAsk.set(false);
    const id = this.providerId();
    if (id === null || this.calibrationStopping()) {
      return;
    }
    const page = this.pageKey();
    this.calibrationStopping.set(true);
    this.calibrationError.set(null);
    try {
      const result = await this.service.stopCalibration(id);
      if (page !== this.pageKey()) {
        return;
      }
      this.notice.set({
        title: result.was_active ? 'Calibration stopped' : 'Nothing to stop',
        text: result.was_active
          ? 'The model in progress stays uncalibrated. Nothing partial is saved.'
          : 'No calibration was running on this node.',
      });
      await this.tryRefresh();
    } catch (e) {
      if (page === this.pageKey()) {
        this.calibrationErrorOf.set('stop');
        this.calibrationError.set(saveFailure(e).message.replace('The configuration could not be saved.', 'The calibration could not be stopped.'));
      }
    } finally {
      if (page === this.pageKey()) {
        this.calibrationStopping.set(false);
      }
    }
  }

  /** Asks the worker to measure this model now. The admin confirmed first. */
  async calibrate(): Promise<void> {
    this.calibrateAsk.set(false);
    const id = this.providerId();
    if (id === null || this.calibrationStarting() || !this.canCalibrate()) {
      return;
    }
    const page = this.pageKey();
    this.calibrationStarting.set(true);
    this.calibrationError.set(null);
    try {
      const result = await this.service.calibrateModel(id, this.modelName());
      if (page !== this.pageKey()) {
        return;
      }
      if (result.count === 0) {
        this.calibrationErrorOf.set('start');
        this.calibrationError.set(result.message || 'This model does not need calibration.');
      } else {
        this.notice.set({
          title: 'Calibration started',
          text: 'The worker measures this model now. This takes several minutes; the page shows the result when it is done.',
        });
      }
      // Whether the status can be read again does not change that the calibration started.
      await this.tryRefresh();
    } catch (e) {
      if (page === this.pageKey()) {
        this.calibrationErrorOf.set('start');
        this.calibrationError.set(saveFailure(e).message.replace('The configuration could not be saved.', 'The calibration could not be started.'));
      }
    } finally {
      if (page === this.pageKey()) {
        this.calibrationStarting.set(false);
      }
    }
  }

  /** After a conflict: take the newer list and leave the editor. */
  async reloadAfterConflict(): Promise<void> {
    const page = this.pageKey();
    this.saving.set(true);
    try {
      await this.refresh();
      if (page !== this.pageKey()) {
        return;
      }
      this.editing.set(false);
      this.editRevision = null;
      this.draft.set(null);
      this.adv.set(null);
      this.failure.set(null);
    } catch (e) {
      if (page === this.pageKey()) {
        this.failure.set(saveFailure(e));
      }
    } finally {
      if (page === this.pageKey()) {
        this.saving.set(false);
      }
    }
  }

  /** Loads config and insights again; the answer counts only while no newer request was made. */
  private async refresh(): Promise<void> {
    const token = ++this.loadToken;
    const id = this.providerId()!;
    try {
      const [config, insights] = await Promise.all([this.service.getNodeConfig(id), this.service.getNodeInsights(id)]);
      if (token !== this.loadToken) {
        return;
      }
      this.config.set(config);
      this.insights.set(insights);
      this.loading.set(false);
    } catch (e) {
      if (token === this.loadToken) {
        this.loading.set(false);
      }
      throw e;
    }
  }
}
