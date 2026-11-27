import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import type { PublicHourlyLoad, PublicHourlySlot } from './public-stats.service';

@Component({
  selector: 'app-public-stats-hourly',
  templateUrl: './public-stats.hourly.html',
  styleUrl: './public-stats.hourly.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PublicStatsHourly {
  readonly load = input<PublicHourlyLoad | null>(null);
  protected readonly selected = signal<number | null>(null);
  protected readonly preview = signal<number | null>(null);
  protected readonly slots = computed(() => this.load()?.hours ?? []);
  protected readonly activeSlot = computed(() => {
    const activeHour = this.preview() ?? this.selected();
    return this.slots().find(s => s.hour === activeHour) ?? null;
  });
  protected readonly maximum = computed(() => {
    const band = this.load()?.band_size ?? 0;
    return Math.max(1, ...this.slots().map(s => s.band_start === null ? 0 : s.band_start + band));
  });

  protected hourLabel(hour: number): string {
    return (hour % 24).toString().padStart(2, '0') + ':00';
  }

  protected slotLabel(slot: PublicHourlySlot): string {
    if (slot.zero) return '0 logged requests';
    if (slot.band_start === null) return 'Not enough combined traffic to publish';
    const upper = slot.band_start + (this.load()?.band_size ?? 0);
    return slot.band_start === 0 ? 'Under ' + upper + ' requests per hour'
      : slot.band_start + ' to <' + upper + ' requests per hour';
  }

  protected heightPercent(slot: PublicHourlySlot): number {
    const band = this.load()?.band_size ?? 0;
    return slot.band_start === null ? 0 : (slot.band_start + band / 2) / this.maximum() * 100;
  }

  protected chooseHour(value: string): void {
    this.preview.set(null);
    this.selected.set(value === '' ? null : Number(value));
  }
}
