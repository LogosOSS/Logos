import { vi } from 'vitest';
import { StatsClockService } from './stats-clock.service';

describe('server-aligned statistics clock', () => {
  afterEach(() => vi.useRealTimers());

  it.each([-300_000, 300_000])('advances independently of browser wall-clock skew (%s ms)', skew => {
    vi.useFakeTimers();
    const server = Date.parse('2026-11-13T12:00:00Z');
    vi.setSystemTime(server + skew);
    const clock = new StatsClockService();
    clock.synchronize(new Date(server).toISOString());
    expect(clock.now()).toBe(server);
    vi.advanceTimersByTime(1500);
    expect(clock.now()).toBe(server + 1500);
    vi.setSystemTime(server - 3600_000);
    expect(clock.now()).toBe(server + 1500);
    vi.advanceTimersByTime(500);
    expect(clock.now()).toBe(server + 2000);
  });

  it('resynchronizes after a paused clock and ignores missing or malformed samples', () => {
    vi.useFakeTimers();
    const clock = new StatsClockService();
    clock.synchronize('2026-11-13T12:00:00Z');
    clock.synchronize('2026-11-13T13:00:00Z');
    clock.synchronize(undefined);
    clock.synchronize('invalid');
    expect(clock.now()).toBe(Date.parse('2026-11-13T13:00:00Z'));
  });

  it('does not go backwards when a server sample spent longer in transit', () => {
    vi.useFakeTimers();
    const clock = new StatsClockService();
    clock.synchronize('2026-11-13T12:00:00Z');
    vi.advanceTimersByTime(1500);
    clock.synchronize('2026-11-13T12:00:01Z');
    expect(clock.now()).toBe(Date.parse('2026-11-13T12:00:01.500Z'));
  });
});
