import {
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  ElementRef,
  inject,
  OnInit,
  signal,
  viewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { combineLatest } from 'rxjs';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { ModalConfirmComponent } from '../../../shared/components/modal/modal-confirm/modal-confirm';
import { NodeConfig, NodeInsights } from '../../../shared/models/node-config.model';
import { CalibrationLabel, calibrationLabel, hasTuning } from '../node-models/node-model-status';
import {
  ADVANCED_FIELDS,
  AdvancedDraft,
  advancedChanges,
  advancedErrors,
  initialAdvanced,
} from './node-model-advanced';
import {
  contextCapacity,
  ErrorItem,
  kvWindowGb,
  Placement,
  saveFailure,
  SaveFailure,
  withoutEntry,
  withPlacement,
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

/** One model on one logosnode: its configuration and what Logos measured. */
@Component({
  selector: 'app-node-model',
  standalone: true,
  imports: [ErrorMessageComponent, ModalConfirmComponent, RouterLink],
  templateUrl: './node-model.html',
  styleUrl: './node-model.scss',
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
  /** The measured result of the calibrated tensor-parallel size. */
  readonly result = computed(() => this.insight()?.calibration.results.find((r) => r.active) ?? null);

  readonly kvGb = computed(() => kvToGb(this.settings()['kv_cache_memory_bytes']));

  // Editing
  readonly editing = signal(false);
  readonly draft = signal<Placement | null>(null);
  readonly adv = signal<AdvancedDraft | null>(null);
  private readonly advStart = signal<AdvancedDraft | null>(null);
  readonly fields = ADVANCED_FIELDS;
  readonly saving = signal(false);
  readonly failure = signal<SaveFailure | null>(null);
  readonly notice = signal<{ title: string; text: string } | null>(null);
  readonly removeOpen = signal(false);
  readonly removing = signal(false);
  readonly removeError = signal<string | null>(null);
  private readonly failureBanner = viewChild<ElementRef<HTMLElement>>('failureBanner');

  /** A calibrated model can be placed while its worker can check the list. */
  readonly canEdit = computed(() => this.workerOnline() && this.result() !== null);
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
    if (!d) return [];
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

  ngOnInit(): void {
    combineLatest([this.route.paramMap, this.route.queryParamMap])
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(([params, query]) => {
        this.providerId.set(Number(params.get('providerId')));
        this.modelName.set(query.get('model') ?? '');
        void this.load();
      });
  }

  async load(): Promise<void> {
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
      this.config.set(config);
      this.insights.set(insights);
      this.providerName.set(providers.find((p) => p.id === id)?.name ?? `Node ${id}`);
    } catch (e) {
      this.error.set(this.messageOf(e));
    } finally {
      this.loading.set(false);
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
    const start = this.initialPlacement();
    if (!start || !this.canEdit()) {
      return;
    }
    this.draft.set(start);
    const settings = initialAdvanced(this.entry());
    this.adv.set(settings);
    this.advStart.set(structuredClone(settings));
    this.failure.set(null);
    this.notice.set(null);
    this.editing.set(true);
  }

  cancelEdit(): void {
    if (this.saving()) {
      return;
    }
    this.editing.set(false);
    this.draft.set(null);
    this.adv.set(null);
    this.failure.set(null);
  }

  setTp(tp: number): void {
    const d = this.draft();
    if (d && this.result()?.tp === tp) {
      this.draft.set({ ...d, tp });
    }
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
    const n = Math.round(Number(value) / 1024) * 1024;
    if (!d || !Number.isFinite(n)) {
      return;
    }
    const cap = this.capacity();
    this.draft.set({ ...d, ctx: Math.max(1024, cap ? Math.min(n, cap) : n) });
  }

  applyRecommendation(): void {
    this.startEdit();
    const d = this.draft();
    const best = this.recommendation()?.settings ?? {};
    if (!d || !this.window()) {
      return;
    }
    this.setKv(kvToGb(best['kv_cache_memory_bytes']) ?? d.kvGb);
    this.setCtx(Number(best['max_model_len']) || this.draft()!.ctx);
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
    if (id === null || !cfg || !d || this.saving()) {
      return;
    }
    this.saving.set(true);
    this.failure.set(null);
    try {
      await this.service.replaceNodeConfig(
        id,
        cfg.revision,
        withPlacement(cfg.entries, this.modelName(), d, this.adv() ?? undefined),
      );
      await this.refresh();
      this.editing.set(false);
      this.draft.set(null);
      this.adv.set(null);
      this.notice.set({ title: 'Saved', text: 'The worker applies the new settings from the next lane start.' });
    } catch (e) {
      this.failure.set(saveFailure(e));
      queueMicrotask(() => this.failureBanner()?.nativeElement.focus());
    } finally {
      this.saving.set(false);
    }
  }

  openRemove(): void {
    this.removeError.set(null);
    this.removeOpen.set(true);
  }

  closeRemove(): void {
    if (!this.removing()) {
      this.removeOpen.set(false);
    }
  }

  /** Takes the model out of the list; its calibration stays. */
  async confirmRemove(): Promise<void> {
    const id = this.providerId();
    const cfg = this.config();
    if (id === null || !cfg || this.removing()) {
      return;
    }
    this.removing.set(true);
    this.removeError.set(null);
    try {
      await this.service.replaceNodeConfig(id, cfg.revision, withoutEntry(cfg.entries, this.modelName()));
      await this.refresh();
      this.removeOpen.set(false);
      this.notice.set({
        title: 'Configuration removed',
        text: 'The model stays calibrated. The worker unloads it at the next lane change.',
      });
    } catch (e) {
      this.removeError.set(saveFailure(e).message);
    } finally {
      this.removing.set(false);
    }
  }

  /** After a conflict: take the newer list and leave the editor. */
  async reloadAfterConflict(): Promise<void> {
    this.saving.set(true);
    try {
      await this.refresh();
      this.editing.set(false);
      this.draft.set(null);
      this.adv.set(null);
      this.failure.set(null);
    } catch (e) {
      this.failure.set(saveFailure(e));
    } finally {
      this.saving.set(false);
    }
  }

  private async refresh(): Promise<void> {
    const id = this.providerId()!;
    const [config, insights] = await Promise.all([this.service.getNodeConfig(id), this.service.getNodeInsights(id)]);
    this.config.set(config);
    this.insights.set(insights);
  }

  private messageOf(e: unknown): string {
    const body = (e as { error?: { error?: string }; message?: string } | null) ?? null;
    return body?.error?.error || body?.message || 'This model could not be loaded.';
  }
}
