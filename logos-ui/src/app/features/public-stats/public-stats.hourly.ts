import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import type { PublicLocalArrivalHour, PublicLocalRequestArrivals } from './public-stats.service';

const HOURS_PER_PAGE = 24;
const berlin = { timeZone: 'Europe/Berlin', hourCycle: 'h23' } as const;
const dayTime = new Intl.DateTimeFormat('en-GB', { ...berlin, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const hourTick = new Intl.DateTimeFormat('en-GB', { ...berlin, hour: '2-digit', minute: '2-digit' });
const hourDetail = new Intl.DateTimeFormat('en-GB', {
  ...berlin, day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZoneName: 'shortOffset',
});
const counts = new Intl.NumberFormat('en-GB');

@Component({
  selector: 'app-public-stats-hourly',
  templateUrl: './public-stats.hourly.html',
  styleUrl: './public-stats.hourly.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicStatsHourly {
  readonly load = input<PublicLocalRequestArrivals | null>(null);
  protected readonly includeAgent = signal(false);
  protected readonly includeBenchmarks = signal(false);
  protected readonly daysBack = signal(0);
  protected readonly selected = signal<string | null>(null);
  protected readonly preview = signal<string | null>(null);
  protected readonly hasOlder = computed(() => (this.daysBack() + 1) * HOURS_PER_PAGE < (this.load()?.hours.length ?? 0));
  protected readonly slots = computed(() => {
    const hours = this.load()?.hours ?? [];
    const end = hours.length - this.daysBack() * HOURS_PER_PAGE;
    return end <= 0 ? [] : hours.slice(Math.max(0, end - HOURS_PER_PAGE), end);
  });
  protected readonly activeSlot = computed(() => {
    const start = this.preview() ?? this.selected();
    return this.slots().find(s => s.start === start);
  });
  protected readonly maximum = computed(() => Math.max(1, ...this.slots().map(s => this.total(s))));
  protected readonly period = computed(() => {
    const slots = this.slots();
    if (!slots.length) return '';
    const end = new Date(Date.parse(slots[slots.length - 1].start) + 3_600_000);
    return dayTime.format(new Date(slots[0].start)) + ' to ' + dayTime.format(end) + ' · Europe/Berlin';
  });

  protected page(direction: number): void {
    this.selected.set(null);
    this.preview.set(null);
    this.daysBack.update(days => days + direction);
  }

  protected tick(start: string): string {
    return hourTick.format(new Date(start));
  }

  protected detail(start: string): string {
    return hourDetail.format(new Date(start));
  }

  protected total(slot: PublicLocalArrivalHour): number {
    return slot.regular + (this.includeAgent() ? slot.agent : 0) + (this.includeBenchmarks() ? slot.benchmark : 0);
  }

  protected count(value: number): string {
    return counts.format(value);
  }
}
