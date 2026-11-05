import { describe, expect, it } from 'vitest';

import { compareTimestamps } from './billing.service';

describe('compareTimestamps', () => {
  it('orders ISO-8601 bucket timestamps chronologically', () => {
    const times = ['2026-03-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z'];
    expect([...times].sort(compareTimestamps)).toEqual([
      '2026-01-01T00:00:00Z',
      '2026-02-01T00:00:00Z',
      '2026-03-01T00:00:00Z',
    ]);
  });

  it('treats identical timestamps as equal', () => {
    expect(compareTimestamps('2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')).toBe(0);
  });
});
