import { Component, Input, OnChanges, OnDestroy, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { FormsModule } from '@angular/forms';
import { ModalConfirmComponent } from '../../shared/components/modal/modal-confirm/modal-confirm';
import { requestDatasetLabel } from './benchmark-configuration';

interface SavedWorkload { id: string; name: string; request_count: number; provenance: string; created_at?: string; }

export interface NightlyStatus {
  workload_policy?: { kind: 'selected' | 'recent_user'; username?: string; request_count?: number; ready?: boolean; error?: string | null };
  workload_id?: string | null;
  workloads?: SavedWorkload[];
  optimizer?: { production_adoption?: { benchmark_id: number; baseline_id: number; baseline_throughput: number; throughput: number }; reference_id?: number; best_benchmark_id?: number; iterations?: number; nightly_session?: { date: string; rounds: number; successful_configurations: number } };
  max_maintenance_nodes?: number;
  window?: { start: string; end: string; benchmark_start_cutoff: string; timezone: string };
  enabled: boolean;
  status: string;
  recovery_required: boolean;
  worker_ready?: boolean;
  last_run_date?: string;
  plan?: { serving_overrides: Record<string, number | null> }[];
  report: { configuration?: number; attempt?: number; status: string; reason?: string; benchmark_id?: number; job_id?: number }[];
}

@Component({
  selector: 'app-nightly-benchmarks',
  standalone: true,
  imports: [ModalConfirmComponent, FormsModule],
  templateUrl: './nightly-benchmarks.html',
  styleUrl: './nightly-benchmarks.scss',
})
export class NightlyBenchmarks implements OnChanges, OnDestroy {
  readonly datasetLabel = requestDatasetLabel;
  @Input({ required: true }) pairId!: number;
  @Input({ required: true }) providerName!: string;
  private readonly http = inject(HttpClient);
  readonly state = signal<NightlyStatus | null>(null);
  readonly confirming = signal(false);
  readonly saving = signal(false);
  readonly error = signal<string | null>(null);
  selectedWorkload: string | null = null;
  sourceEdited = false;
  lookbackDays: 1 | 3 | 7 | null = null;
  readonly sourceNotice = signal<string | null>(null);
  workloadName = '';
  logIds = '';
  provenance = 'synthetic';
  private version = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private destroyed = false;

  ngOnChanges(): void {
    this.version++;
    this.state.set(null);
    this.selectedWorkload = null;
    this.sourceEdited = false;
    this.lookbackDays = null;
    this.sourceNotice.set(null);
    this.saving.set(false);
    this.error.set(null);
    this.workloadName = '';
    this.logIds = '';
    this.confirming.set(false);
    clearTimeout(this.timer);
    void this.refresh();
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    this.version++;
    clearTimeout(this.timer);
  }

  async refresh(): Promise<void> {
    const version = this.version;
    try {
      const state = await firstValueFrom(this.http.post<NightlyStatus>(
        '/api/logosdb/model_benchmarks/nightly/status', { model_provider_id: this.pairId }));
      if (version !== this.version || this.destroyed) return;
      this.state.set(state);
      if (!this.sourceEdited) this.selectedWorkload = state.workload_id ?? null;
      if (this.error() === 'Could not load nightly benchmark status.') this.error.set(null);
    } catch {
      if (version !== this.version || this.destroyed) return;
      this.error.set('Could not load nightly benchmark status.');
    } finally {
      if (version === this.version && !this.destroyed) {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => void this.refresh(), 15000);
      }
    }
  }

  selectWorkload(id: string | null): void {
    this.selectedWorkload = id;
    this.sourceEdited = id !== (this.state()?.workload_id ?? null);
    this.sourceNotice.set(null);
    this.error.set(null);
  }

  get enableBlocker(): string | null {
    const state = this.state();
    if (!state || state.enabled) return null;
    if (state.recovery_required) return 'Wait for worker recovery to finish.';
    if (!state.worker_ready) return 'Connect a worker that supports nightly benchmarks.';
    if (!state.window) return 'Waiting for the maintenance window settings.';
    if (this.sourceEdited) return 'Apply your dataset to enable nightly runs.';
    return null;
  }

  get window() {
    return this.state()?.window;
  }

  get automaticSource(): boolean { return this.state()?.workload_policy?.kind === 'recent_user'; }
  get sourceLocked(): boolean { return this.automaticSource || this.saving() || !!this.state()?.recovery_required || ['running', 'preparing', 'continuing'].includes(this.state()?.status ?? ''); }

  async saveSource(): Promise<void> {
    const current = this.state();
    if (!current || this.sourceLocked) return;
    const version = ++this.version;
    this.saving.set(true);
    clearTimeout(this.timer);
    try {
      let workloadId = this.selectedWorkload;
      let created: SavedWorkload | undefined;
      if (workloadId === 'recent') {
        created = await firstValueFrom(this.http.post<SavedWorkload>(
          '/api/logosdb/model_benchmarks/workloads/create', { model_provider_id: this.pairId, lookback_days: this.lookbackDays }));
        if (version !== this.version || this.destroyed) return;
        workloadId = created.id;
        this.selectedWorkload = created.id;
        this.state.set({ ...current, workloads: [created, ...(current.workloads ?? []).filter(row => row.id !== workloadId)] });
      }
      const state = await firstValueFrom(this.http.post<NightlyStatus>('/api/logosdb/model_benchmarks/nightly/configure', {
        model_provider_id: this.pairId, enabled: current.enabled, expected_enabled: current.enabled,
        workload_id: workloadId, expected_workload_id: current.workload_id ?? null,
      }));
      if (version !== this.version || this.destroyed) return;
      this.state.set({ ...this.state()!, ...state });
      this.sourceNotice.set(created ? `Dataset applied · ${created.request_count} requests` : 'Dataset applied.');
      this.sourceEdited = false;
      this.error.set(null);
      await this.refresh();
    } catch (error) {
      if (version === this.version && !this.destroyed) this.showError(error);
    } finally { if (version === this.version) this.saving.set(false); }
  }

  async createWorkload(): Promise<void> {
    if (this.sourceLocked || !this.state()) return;
    const ids = this.logIds.trim().split(/[\s,]+/).map(Number);
    if (!this.workloadName.trim() || !ids.length || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) {
      this.error.set('Enter a name and positive request IDs separated by commas.');
      return;
    }
    const version = this.version;
    this.saving.set(true);
    try {
      const workload = await firstValueFrom(this.http.post<{ id: string }>('/api/logosdb/model_benchmarks/workloads/create', {
        name: this.workloadName.trim(), log_ids: ids, provenance: this.provenance,
      }));
      if (version !== this.version || this.destroyed) return;
      this.selectedWorkload = workload.id;
      this.sourceEdited = true;
      this.error.set(null);
      await this.refresh();
    } catch (error) {
      if (version === this.version && !this.destroyed) this.showError(error);
    } finally { if (version === this.version) this.saving.set(false); }
  }

  private showError(error: unknown): void {
    this.error.set(error instanceof HttpErrorResponse && (typeof error.error?.detail === 'string' || typeof error.error?.error === 'string')
      ? (error.error.detail ?? error.error.error) : 'Could not update the request workload. Refresh and try again.');
    clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.refresh(), 15000);
  }

  statusLabel(value: string): string {
    const labels: Record<string, string> = {
      scheduled: 'Waiting in nightly rotation', unavailable: 'Waiting for an available worker', preparing: 'Preparing worker', running: 'Benchmark in progress',
      continuing: 'Ready for another optimization round', completed: 'Finished — see results', disabled: 'Deactivated', success: 'Successful', failed: 'Failed',
      stalled: 'Stopped after five minutes without progress', preparation_timeout: 'Preparation timed out',
      window_closed: this.window ? `No new runs after ${this.window.benchmark_start_cutoff}` : 'Benchmark start window closed', window_closed_below_half: this.window ? `Stopped at ${this.window.end} below 50% complete` : 'Stopped below 50% at the maintenance cutoff',
      calibration_pending: 'Waiting for calibration', adopted: 'Applied to production', adoption_failed: 'Adoption failed; restoring original settings',
      recovery_required: 'Worker recovery required', interrupted_recovered: 'Interrupted; worker restored',
      interrupted: 'Interrupted', cancelled: 'Cancelled', lease_lost: 'Maintenance ownership lost',
    };
    return labels[value] ?? value;
  }

  async toggleNightly(): Promise<void> {
    const state = this.state();
    if (!state || this.saving() || this.enableBlocker) return;
    this.error.set(null);
    if (state.enabled) {
      this.confirming.set(true);
    } else {
      await this.setEnabled(true);
    }
  }

  async confirmDisable(): Promise<void> {
    if (this.confirming() && !this.saving()) await this.setEnabled(false);
  }

  private async setEnabled(enabled: boolean): Promise<void> {
    const version = ++this.version;
    this.saving.set(true);
    clearTimeout(this.timer);
    try {
      const state = await firstValueFrom(this.http.post<NightlyStatus>(
        '/api/logosdb/model_benchmarks/nightly/configure', {
          model_provider_id: this.pairId, enabled, expected_enabled: !enabled,
        }));
      if (version !== this.version || this.destroyed) return;
      this.state.set({ ...this.state(), ...state, plan: state.plan ?? this.state()?.plan, window: state.window ?? this.state()?.window, max_maintenance_nodes: state.max_maintenance_nodes ?? this.state()?.max_maintenance_nodes });
      this.confirming.set(false);
      this.error.set(null);
    } catch (error) {
      if (version !== this.version || this.destroyed) return;
      this.error.set(error instanceof HttpErrorResponse && typeof error.error?.detail === 'string'
        ? error.error.detail : 'Could not change nightly benchmarks. Refresh and try again.');
    } finally {
      if (version === this.version && !this.destroyed) {
        this.saving.set(false);
        this.timer = setTimeout(() => void this.refresh(), 15000);
      }
    }
  }
}
