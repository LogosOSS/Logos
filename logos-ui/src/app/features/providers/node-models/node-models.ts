import { ChangeDetectionStrategy, Component, computed, inject, input, OnInit, signal } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ProviderManagementService } from '../../../core/services/provider-management.service';
import { NodeInsights, NodeModelInsight } from '../../../shared/models/node-config.model';
import { DataTableComponent } from '../../../shared/components/data-table/data-table';
import { ErrorMessageComponent } from '../../../shared/components/error-message/error-message';
import { CalibrationLabel, calibrationLabel, hasTuning } from './node-model-status';

/** The models of one logosnode: calibration and configuration state. */
@Component({
  selector: 'app-node-models',
  standalone: true,
  imports: [DataTableComponent, ErrorMessageComponent, RouterLink],
  templateUrl: './node-models.html',
  styleUrl: './node-models.scss',
  changeDetection: ChangeDetectionStrategy.Eager,
})
export class NodeModels implements OnInit {
  private readonly service = inject(ProviderManagementService);

  readonly providerId = input.required<number>();

  readonly insights = signal<NodeInsights | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);

  readonly models = computed(() => this.insights()?.models ?? []);
  readonly workerOnline = computed(() => this.insights()?.worker.online ?? false);

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
