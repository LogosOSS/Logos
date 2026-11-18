import { ChangeDetectionStrategy, Component, computed, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { NodeGpuInsight, NodeSettings, NodeSettingsSaved, NodeSettingsValues } from '../../../shared/models/node-config.model';
import { saveFailure, SaveFailure } from '../node-model/node-model-edit';
import {
  draftErrors,
  GpuMode,
  initialDraft,
  MAX_POLL_SECONDS,
  remeasures,
  SettingsDraft,
  settingsChanges,
  toSettings,
  Toggle,
} from './node-settings-edit';

/** The settings of a whole logosnode that belong to no model. */
@Component({
  selector: 'app-node-settings',
  standalone: true,
  imports: [ErrorMessageComponent, RouterLink],
  templateUrl: './node-settings.html',
  styleUrl: '../node-model/node-model.scss',
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeSettingsPage implements OnInit {
  private readonly service = inject(ProviderManagementService);
  private readonly route = inject(ActivatedRoute);
  private readonly destroyRef = inject(DestroyRef);

  readonly maxPoll = MAX_POLL_SECONDS;
  readonly providerId = signal<number | null>(null);
  readonly providerName = signal('');
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly stored = signal<NodeSettings | null>(null);
  readonly gpus = signal<NodeGpuInsight[]>([]);
  readonly workerOnline = signal(false);
  readonly draft = signal<SettingsDraft | null>(null);
  readonly saving = signal(false);
  readonly failure = signal<SaveFailure | null>(null);
  readonly notice = signal<{ title: string; text: string } | null>(null);

  readonly errors = computed(() => {
    const d = this.draft();
    return d ? draftErrors(d) : {};
  });
  readonly hasErrors = computed(() => Object.keys(this.errors()).length > 0);
  private readonly start = computed<NodeSettingsValues>(() => this.stored()?.settings ?? {});
  private readonly current = computed<NodeSettingsValues>(() => {
    const d = this.draft();
    return d ? toSettings(d) : {};
  });
  readonly changes = computed(() => settingsChanges(this.start(), this.current()));
  readonly dirty = computed(() => this.changes().length > 0);
  readonly remeasuring = computed(() => remeasures(this.start(), this.current()));

  ngOnInit(): void {
    this.route.paramMap.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((params) => {
      this.providerId.set(Number(params.get('providerId')));
      void this.load();
    });
  }

  async load(): Promise<void> {
    const id = this.providerId();
    if (id === null || Number.isNaN(id)) {
      this.loading.set(false);
      this.error.set('This page needs a node.');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    try {
      const [settings, insights, providers] = await Promise.all([
        this.service.getNodeSettings(id),
        this.service.getNodeInsights(id),
        this.service.getProviders(),
      ]);
      this.stored.set(settings);
      this.gpus.set(insights.gpus);
      this.workerOnline.set(insights.worker.online);
      this.providerName.set(providers.find((p) => p.id === id)?.name ?? `Node ${id}`);
      this.draft.set(initialDraft(settings.settings));
    } catch (e) {
      const body = e as { error?: { error?: string }; message?: string } | null;
      this.error.set(body?.error?.error || body?.message || 'The settings of this node could not be loaded.');
    } finally {
      this.loading.set(false);
    }
  }

  setMode(mode: GpuMode): void {
    this.patch({ gpuMode: mode });
  }

  /** Whether a GPU is in the pool: "all" and the default count every GPU in. */
  gpuChosen(index: number): boolean {
    const d = this.draft();
    if (!d || d.gpuMode === 'none') {
      return false;
    }
    return d.gpuMode === 'pick' ? d.gpus.includes(index) : true;
  }

  /** A tile adds or drops one GPU; all GPUs make it "all" and no GPU makes it "none". */
  toggleGpu(index: number): void {
    const d = this.draft();
    if (!d) {
      return;
    }
    const all = this.gpus().map((g) => g.index);
    const chosen = all.filter((g) => (g === index ? !this.gpuChosen(g) : this.gpuChosen(g)));
    if (all.length > 0 && chosen.length === all.length) {
      this.patch({ gpuMode: 'all', gpus: [] });
    } else if (chosen.length === 0) {
      this.patch({ gpuMode: 'none', gpus: [] });
    } else {
      this.patch({ gpuMode: 'pick', gpus: chosen });
    }
  }

  gpuMemory(gpu: NodeGpuInsight): string {
    return gpu.total_mb ? `${Math.round(gpu.total_mb / 1024)} GB` : 'unknown';
  }

  /** "4 × 40 GB" for equal GPUs, else the count. */
  readonly gpuSummary = computed(() => {
    const gpus = this.gpus();
    if (gpus.length === 0) {
      return 'None reported';
    }
    const sizes = new Set(gpus.map((g) => this.gpuMemory(g)));
    return sizes.size === 1 ? `${gpus.length} × ${[...sizes][0]}` : `${gpus.length} GPUs`;
  });

  setArgs(value: string): void {
    this.patch({ args: value });
  }

  setReboot(value: string): void {
    this.patch({ reboot: value as Toggle });
  }

  setPoll(value: string): void {
    this.patch({ poll: value });
  }

  reset(): void {
    this.draft.set(initialDraft(this.start()));
    this.failure.set(null);
  }

  async save(): Promise<void> {
    const id = this.providerId();
    const stored = this.stored();
    if (id === null || !stored || this.saving() || !this.dirty() || this.hasErrors()) {
      return;
    }
    this.saving.set(true);
    this.failure.set(null);
    const saved = this.current();
    let answer: NodeSettingsSaved;
    try {
      answer = await this.service.replaceNodeSettings(id, stored.revision, saved);
    } catch (e) {
      this.failure.set(saveFailure(e));
      this.saving.set(false);
      return;
    }
    // The document is stored. Read it again for the exact state; if that fails, the saved values stand.
    let fresh: NodeSettings = { ...stored, revision: answer.settings_revision, settings: saved };
    try {
      fresh = await this.service.getNodeSettings(id);
    } catch {
      // Keep what was saved; the next save checks the revision again.
    }
    const remeasure = remeasures(stored.settings, saved);
    this.stored.set(fresh);
    this.draft.set(initialDraft(fresh.settings));
    this.notice.set({
      title: 'Saved',
      text: this.appliedText(answer) + (remeasure ? ' Every model is measured again in the next calibration window.' : ''),
    });
    this.saving.set(false);
  }

  /** What happened to the settings on the worker, as the answer of the save reports it. */
  private appliedText(answer: NodeSettingsSaved): string {
    if (answer.deferred) {
      return 'The worker applies the settings when its running calibration ends.';
    }
    if (!answer.applied) {
      return 'The worker applies the settings when it connects.';
    }
    const restart = answer.restart_needed ?? [];
    return restart.length > 0
      ? `The worker applies the settings now. ${restart.join(', ')} take effect at the next start of the worker.`
      : 'The worker applies the settings now.';
  }

  /** After a conflict: take the newer document. */
  async reloadAfterConflict(): Promise<void> {
    this.failure.set(null);
    await this.load();
  }

  fieldError(key: string): string | null {
    return this.errors()[key] ?? this.failure()?.items.find((item) => item.key === key)?.message ?? null;
  }

  private patch(change: Partial<SettingsDraft>): void {
    const d = this.draft();
    if (d) {
      this.draft.set({ ...d, ...change });
      this.notice.set(null);
    }
  }
}
