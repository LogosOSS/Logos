import { ChangeDetectionStrategy, Component, computed, inject, input, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ModelManagementService } from '../../../core/services/model-management.service';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { DataTableComponent } from '../../../shared/components/data-table/data-table';
import { ModalFormComponent } from '../../../shared/components/modal/modal-form/modal-form';
import { AppSelectOption, SelectComponent } from '../../../shared/components/select/select';
import { loadFailure, saveFailure } from '../node-model/node-model-edit';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { describePlacement } from '../node-model/node-model-format';
import { CalibrationLabel, calibrationLabel, hasTuning, PlacementLabel, placementLabel } from './node-model-status';

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

  /** The latest request for the list; an answer to an older one is dropped. */
  private loadToken = 0;

  async load(): Promise<void> {
    const token = ++this.loadToken;
    this.loading.set(true);
    this.error.set(null);
    try {
      const insights = await this.service.getNodeInsights(this.providerId());
      if (token === this.loadToken) {
        this.insights.set(insights);
      }
    } catch (e) {
      if (token === this.loadToken) {
        this.error.set(loadFailure(e, 'The models of this node could not be loaded.'));
      }
    } finally {
      if (token === this.loadToken) {
        this.loading.set(false);
      }
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
    const id = this.providerId();
    this.connecting.set(true);
    this.connectError.set(null);
    try {
      const config = await this.service.getNodeConfig(id);
      const entries = [...config.entries, { model_name: model, capability: {}, overrides: {} }];
      await this.service.replaceNodeConfig(id, config.revision, entries);
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

  describePlacement(settings: Record<string, unknown>): string {
    return describePlacement(settings);
  }

  placementLabel(model: NodeModelInsight): PlacementLabel {
    return placementLabel(model);
  }

  hasTuning(model: NodeModelInsight): boolean {
    return hasTuning(model);
  }
}
