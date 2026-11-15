import { Component, Input, OnChanges, OnDestroy, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { RequestRouting, RoutingCandidate, StatisticsService } from '../../services/statistics.service';

@Component({
  selector: 'app-request-routing',
  imports: [DecimalPipe],
  templateUrl: './request-routing.html',
  styleUrl: './request-routing.scss',
})
export class RequestRoutingComponent implements OnChanges, OnDestroy {
  @Input({ required: true }) requestId = '';
  @Input() complete = false;
  @Input() providerName = '';

  private readonly statistics = inject(StatisticsService);
  private generation = 0;
  readonly routing = signal<RequestRouting | null>(null);
  readonly loading = signal(false);
  readonly error = signal(false);
  readonly multipleModels = computed(() => new Set(this.routing()?.candidates.map(c => c.model_id)).size > 1);

  ngOnChanges(): void {
    // Completion or rerouting can update the stored decision while this panel is open.
    void this.load();
  }

  ngOnDestroy(): void {
    ++this.generation;
  }

  async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(false);
    this.routing.set(null);
    try {
      const routing = await this.statistics.getRequestRouting(this.requestId);
      if (generation === this.generation) this.routing.set(routing);
    } catch {
      if (generation === this.generation) this.error.set(true);
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  selected(candidate: RoutingCandidate): boolean {
    const routing = this.routing();
    return candidate.provider_id === routing?.selected_provider_id
      && candidate.model_id === routing?.selected_model_id;
  }

  stateLabel(candidate: RoutingCandidate): string {
    if (candidate.provider_type === 'cloud' && candidate.state !== 'unavailable') return 'cloud';
    const labels: Record<string, string> = {
      warm: 'warm', busy: 'busy', sleeping: 'sleeping', cold: 'cold · not loaded',
      cold_reclaim: 'cold · reclaim needed', sleeping_reclaim: 'sleeping · reclaim needed',
      unavailable: 'unavailable',
    };
    return labels[candidate.state] ?? 'unknown';
  }
}
