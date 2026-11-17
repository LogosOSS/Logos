import { ChangeDetectionStrategy, Component, computed, inject, input, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ModelManagementService } from '../../../core/services/model-management.service';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { DataTableComponent } from '../../../shared/components/data-table/data-table';
import { ModalFormComponent } from '../../../shared/components/modal/modal-form/modal-form';
import { AppSelectOption, SelectComponent } from '../../../shared/components/select/select';
import { saveFailure } from '../node-model/node-model-edit';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { CalibrationLabel, calibrationLabel, hasTuning } from './node-model-status';

/** The models of one logosnode: calibration and configuration state. */
@Component({
  selector: 'app-node-models',
  standalone: true,
  imports: [DataTableComponent, ErrorMessageComponent, ModalFormComponent, RouterLink, SelectComponent],
  templateUrl: './node-models.html',
  styleUrl: './node-models.scss',
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeModels implements OnInit {
  private readonly service = inject(ProviderManagementService);
  private readonly catalog = inject(ModelManagementService);

  readonly providerId = input.required<number>();

  readonly insights = signal<NodeInsights | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly models = computed(() => this.insights()?.models ?? []);
  readonly workerOnline = computed(() => this.insights()?.worker.online ?? false);

  // Connect model
  readonly connectOpen = signal(false);
  readonly connectChoice = signal<string | null>(null);
  readonly connecting = signal(false);
  readonly connectError = signal<string | null>(null);
  readonly catalogLoading = signal(false);
  private readonly catalogNames = signal<string[]>([]);

  /** Catalog models this node does not list yet. */
  readonly connectOptions = computed<AppSelectOption[]>(() => {
    const listed = new Set(this.models().map((m) => m.model_name));
    return this.catalogNames()
      .filter((name) => !listed.has(name))
      .map((name) => ({ value: name, label: name }));
  });

  ngOnInit(): void {
    void this.load();
  }

  async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.insights.set(await this.service.getNodeInsights(this.providerId()));
    } catch (e) {
      this.error.set(this.messageOf(e));
    } finally {
      this.loading.set(false);
    }
  }

  async openConnect(): Promise<void> {
    this.connectChoice.set(null);
    this.connectError.set(null);
    this.connectOpen.set(true);
    this.catalogLoading.set(true);
    try {
      this.catalogNames.set((await this.catalog.getModels()).map((m) => m.name).sort());
    } catch {
      this.connectError.set('The model catalog could not be loaded.');
    } finally {
      this.catalogLoading.set(false);
    }
  }

  closeConnect(): void {
    if (!this.connecting()) {
      this.connectOpen.set(false);
    }
  }

  /** Lists the model without a placement; it shows as not calibrated. */
  async submitConnect(): Promise<void> {
    const model = this.connectChoice();
    if (!model || this.connecting()) {
      return;
    }
    this.connecting.set(true);
    this.connectError.set(null);
    try {
      const config = await this.service.getNodeConfig(this.providerId());
      const entries = [...config.entries, { model_name: model, capability: {}, overrides: {} }];
      await this.service.replaceNodeConfig(this.providerId(), config.revision, entries);
      this.connectOpen.set(false);
      await this.load();
    } catch (e) {
      this.connectError.set(saveFailure(e).message);
    } finally {
      this.connecting.set(false);
    }
  }

  calibrationLabel(model: NodeModelInsight): CalibrationLabel {
    return calibrationLabel(model);
  }

  hasTuning(model: NodeModelInsight): boolean {
    return hasTuning(model);
  }

  private messageOf(e: unknown): string {
    const body = (e as { error?: { error?: string }; message?: string } | null) ?? null;
    return body?.error?.error || body?.message || 'The models of this node could not be loaded.';
  }
}
