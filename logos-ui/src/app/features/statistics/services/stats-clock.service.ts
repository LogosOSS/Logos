import { Injectable } from '@angular/core';

/** Server epoch advanced by a monotonic clock, independent of the browser's wall clock. */
@Injectable({ providedIn: 'root' })
export class StatsClockService {
  private anchor: { epoch: number; monotonic: number } | null = null;

  synchronize(serverNow: unknown): void {
    if (typeof serverNow !== 'string') return;
    const epoch = Date.parse(serverNow);
    if (Number.isFinite(epoch)) {
      // A delayed packet must not wind the display backwards. A fresh sample
      // can still advance it after a platform paused performance.now in sleep.
      this.anchor = { epoch: Math.max(epoch, this.anchor ? this.now() : epoch), monotonic: performance.now() };
    }
  }

  now(): number {
    return this.anchor
      ? this.anchor.epoch + Math.max(0, performance.now() - this.anchor.monotonic)
      : Date.now();
  }
}
