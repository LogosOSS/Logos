import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BillingService, BudgetBucket } from '../../core/services/billing.service';
import { Billing } from './billing';

function bucket(bucket_ts: string, cost_micro_cents: number): BudgetBucket {
  return { team_id: 1, team_name: 'research', bucket_ts, cost_micro_cents };
}

describe('Billing', () => {
  let billingService: { getTeamBudgetHistory: ReturnType<typeof vi.fn>; getKeyBudgetHistory: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    billingService = {
      getTeamBudgetHistory: vi.fn().mockResolvedValue({ buckets: [], bucket_seconds: 86400, start_iso: '', end_iso: '' }),
      getKeyBudgetHistory: vi.fn().mockResolvedValue({ buckets: [], bucket_seconds: 86400, start_iso: '', end_iso: '' }),
    };
    await TestBed.configureTestingModule({
      imports: [Billing],
      providers: [{ provide: BillingService, useValue: billingService }],
    })
      .overrideComponent(Billing, { set: { template: '' } })
      .compileComponents();
  });

  it('loads the current and the previous period on start', async () => {
    const fixture = TestBed.createComponent(Billing);
    fixture.detectChanges();
    await fixture.whenStable();
    expect(billingService.getTeamBudgetHistory).toHaveBeenCalledTimes(2);
  });

  it('draws the bars in chronological order whatever order the buckets arrive in', () => {
    const component = TestBed.createComponent(Billing).componentInstance;
    component.currentBuckets.set([bucket('2026-01-03T00:00:00Z', 30), bucket('2026-01-01T00:00:00Z', 10), bucket('2026-01-02T00:00:00Z', 20)]);
    expect(component.chartData().buckets.map((column) => column.total)).toEqual([10, 20, 30]);
  });

  it('loads the keys of a team the first time it is expanded', async () => {
    const component = TestBed.createComponent(Billing).componentInstance;
    component.toggleTeam(7);
    await vi.waitFor(() => expect(billingService.getKeyBudgetHistory).toHaveBeenCalledWith(7, expect.any(String), expect.any(String)));
  });
});
