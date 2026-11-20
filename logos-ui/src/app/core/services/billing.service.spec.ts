import { describe, expect, it } from 'vitest';

import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { BillingService, compareTimestamps } from './billing.service';

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

describe('billing history transport', () => {
  it('uses QUERY with JSON date filters for team and key history', async () => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    const service = TestBed.inject(BillingService);
    const http = TestBed.inject(HttpTestingController);
    const team = service.getTeamBudgetHistory('start', 'end');
    const keys = service.getKeyBudgetHistory(42, 'start', 'end');
    for (const url of ['/api/logosdb/billing/team_budget_history', '/api/logosdb/billing/key_budget_history/42']) {
      const request = http.expectOne({ method: 'QUERY', url });
      expect(request.request.body).toEqual({ start_iso: 'start', end_iso: 'end' });
      request.flush({ buckets: [], bucket_seconds: 86400, start_iso: 'start', end_iso: 'end' });
    }
    await Promise.all([team, keys]);
    http.verify();
  });
});
