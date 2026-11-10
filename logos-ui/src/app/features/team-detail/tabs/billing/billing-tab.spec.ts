import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BillingService, KeyBudgetBucket } from '../../../../core/services/billing.service';
import { BillingTabComponent } from './billing-tab';

function bucket(bucket_ts: string, cost_micro_cents: number): KeyBudgetBucket {
  return { api_key_id: 1, api_key_name: 'ci', bucket_ts, cost_micro_cents };
}

describe('BillingTabComponent', () => {
  let billingService: { getKeyBudgetHistory: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    billingService = {
      getKeyBudgetHistory: vi.fn().mockResolvedValue({ buckets: [], bucket_seconds: 86400, start_iso: '', end_iso: '' }),
    };
    await TestBed.configureTestingModule({
      imports: [BillingTabComponent],
      providers: [{ provide: BillingService, useValue: billingService }],
    })
      .overrideComponent(BillingTabComponent, { set: { template: '' } })
      .compileComponents();
  });

  it('loads the key spend of its team on start', async () => {
    const fixture = TestBed.createComponent(BillingTabComponent);
    fixture.componentInstance.teamId = 42;
    fixture.detectChanges();
    await fixture.whenStable();
    expect(billingService.getKeyBudgetHistory).toHaveBeenCalledWith(42, expect.any(String), expect.any(String));
  });

  it('draws the bars in chronological order whatever order the buckets arrive in', () => {
    const component = TestBed.createComponent(BillingTabComponent).componentInstance;
    component.currentBuckets.set([bucket('2026-01-02T00:00:00Z', 20), bucket('2026-01-01T00:00:00Z', 10)]);
    expect(component.chartData().buckets.map((column) => column.total)).toEqual([10, 20]);
  });
});
