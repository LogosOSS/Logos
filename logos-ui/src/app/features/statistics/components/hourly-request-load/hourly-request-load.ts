import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal } from '@angular/core';
import {
  type HourlyRequestLoad,
  type HourlyRequestSlot,
  StatisticsService,
} from '../../services/statistics.service';
import type { RequestLatencyScope } from '../../services/request-latency.models';
import { ChartPanel } from '../chart-panel/chart-panel';
import { EmptyState } from '../empty-state/empty-state';
import { StatsSkeletonComponent } from '../skeletons/skeletons';

const DAY_GROUPS = [
  { key: 'all_days', label: 'All days' },
  { key: 'weekdays', label: 'Weekdays' },
  { key: 'weekends', label: 'Weekends' },
] as const;
type DayGroup = (typeof DAY_GROUPS)[number]['key'];

const SOURCES = [
  { key: 'other_requests', label: 'Other requests', color: 'other' },
  { key: 'agent_requests', label: 'Logos Agent', color: 'agent' },
  { key: 'benchmark_requests', label: 'Internal benchmarks', color: 'benchmark' },
] as const;
type SourceKey = (typeof SOURCES)[number]['key'];

const formatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

@Component({
  selector: 'app-stats-hourly-request-load',
  imports: [ChartPanel, EmptyState, StatsSkeletonComponent],
  templateUrl: './hourly-request-load.html',
  styleUrl: './hourly-request-load.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class HourlyRequestLoadComponent {
  private readonly stats = inject(StatisticsService);
  readonly start = input.required<string>();
  readonly end = input.required<string>();
  readonly refresh = input(0);
  readonly scope = input.required<RequestLatencyScope>();

  protected readonly groups = DAY_GROUPS;
  protected readonly sources = SOURCES;
  protected readonly showAgent = signal(true);
  protected readonly showBenchmarks = signal(false);
  protected readonly visibleSources = computed(() => this.sources.filter(({ key }) =>
    key === 'other_requests' || (key === 'agent_requests' && this.showAgent())
      || (key === 'benchmark_requests' && this.showBenchmarks()),
  ));
  protected readonly group = signal<DayGroup>('all_days');
  protected readonly picked = signal<number | null>(null);
  protected readonly preview = signal<number | null>(null);
  protected readonly request = signal<
    { state: 'loading' | 'error' } | { state: 'ready'; data: HourlyRequestLoad }
  >({ state: 'loading' });
  private readonly retryCounter = signal(0);

  protected retry(): void {
    this.retryCounter.update(n => n + 1);
  }
  protected readonly result = computed(() => {
    const value = this.request();
    return value.state === 'ready' ? value.data : null;
  });
  protected readonly slots = computed(() => this.result()?.[this.group()] ?? []);
  protected readonly hasCompleteHours = computed(() => this.slots().some((slot) => slot.complete_hours > 0));
  protected readonly maximum = computed(() => {
    const result = this.result();
    return result ? Math.max(0, ...this.groups.flatMap(({ key }) => result[key].map(s => this.average(s)))) : 0;
  });

  protected readonly detail = computed(() => {
    const slots = this.slots();
    const selected = this.preview() ?? this.picked();
    const observed = slots.filter(slot => slot.complete_hours > 0);
    return observed.find(slot => slot.hour === selected)
      ?? observed.reduce<HourlyRequestSlot | undefined>(
        (peak, slot) => !peak || this.average(slot) > this.average(peak) ? slot : peak, undefined,
      );
  });

  constructor() {
    effect((onCleanup) => {
      const start = this.start();
      const end = this.end();
      const scope = this.scope();
      this.retryCounter();
      this.refresh();
      this.request.set({ state: 'loading' });
      this.preview.set(null);
      const subscription = this.stats.getHourlyRequestLoad(start, end, scope).subscribe({
        next: (data) => this.request.set({ state: 'ready', data }),
        error: () => this.request.set({ state: 'error' }),
      });
      onCleanup(() => subscription.unsubscribe());
    });
  }

  protected selectGroup(group: DayGroup): void {
    this.preview.set(null);
    this.picked.set(null);
    this.group.set(group);
  }

  protected label(hour: number): string {
    return String(hour).padStart(2, '0') + ':00';
  }

  protected average(slot: HourlyRequestSlot, key?: SourceKey): number {
    if (!slot.complete_hours) return 0;
    const count = key ? slot[key] : this.visibleSources().reduce((sum, source) => sum + slot[source.key], 0);
    return count / slot.complete_hours;
  }

  protected format(n: number): string {
    return n > 0 && n < 0.01 ? '<0.01' : formatter.format(n);
  }

  protected meter(slot: HourlyRequestSlot, key: SourceKey): number {
    return this.maximum() > 0 ? this.average(slot, key) * 100 / this.maximum() : 0;
  }

  protected description(slot: HourlyRequestSlot): string {
    const window = this.label(slot.hour) + ' to ' + this.label((slot.hour + 1) % 24);
    return !slot.complete_hours ? window + ': no complete hours in range'
      : window + ': ' + this.format(this.average(slot)) + ' average logged requests across '
        + slot.complete_hours + (slot.complete_hours === 1 ? ' complete hour' : ' complete hours');
  }
}
