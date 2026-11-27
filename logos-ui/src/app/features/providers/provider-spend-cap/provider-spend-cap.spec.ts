import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ProviderSpendStatus } from '../../../shared/models/provider.model';
import { ProviderSpendCap } from './provider-spend-cap';

const url = '/api/logosdb/providers/7/monthly-spend-cap';
const status: ProviderSpendStatus = {
  current: { enabled: true, effective_month: '2026-11-01', limit_usd: '400.00' },
  scheduled: { enabled: true, effective_month: '2026-12-01', limit_usd: '500.00' },
  charged_usd: '87.25',
  reserved_usd: '10.5',
  bound_violation: false,
  bounded_deployments: 0,
};

describe('ProviderSpendCap', () => {
  let fixture: ComponentFixture<ProviderSpendCap>;
  let http: HttpTestingController;
  let host: HTMLElement;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(ProviderSpendCap);
    fixture.componentRef.setInput('providerId', 7);
    host = fixture.nativeElement;
    fixture.detectChanges();
  });

  afterEach(() => http.verify());

  async function detect(): Promise<void> {
    await fixture.whenStable();
    fixture.detectChanges();
  }

  async function load(value = status): Promise<void> {
    http.expectOne({ method: 'GET', url }).flush(value);
    await detect();
  }

  function button(name: string): HTMLButtonElement {
    return [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === name)!;
  }

  function typeLimit(value: string): void {
    const input = host.querySelector<HTMLInputElement>('input[inputmode="decimal"]')!;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  it('shows the current month, next UTC month, reservations and unsupported state', async () => {
    expect(host.textContent).toContain('Loading spend cap');
    await load();
    for (const text of ['$400.00', '$87.25', '$10.50', 'December 1, 2026 (UTC)', '$500.00 limit', 'No deployments have a verified cost bound']) {
      expect(host.textContent).toContain(text);
    }
  });

  it('keeps loading errors distinct from an uncapped response and allows retry', async () => {
    http.expectOne({ method: 'GET', url }).flush(null, { status: 503, statusText: 'Unavailable' });
    await detect();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('Could not load');
    expect(host.textContent).not.toContain('No cap');
    button('Retry').click();
    await load({ ...status, current: null, scheduled: null });
    expect(host.textContent).toContain('No cap');
  });

  it('edits the scheduled value, rejects excess precision and sends an exact decimal string', async () => {
    await load();
    button('Edit spend cap').click();
    fixture.detectChanges();
    expect(host.querySelector<HTMLInputElement>('[inputmode="decimal"]')?.value).toBe('500');
    for (const invalid of ['-1', '1.000000001', '9e9', '92233720368.54775808']) {
      typeLimit(invalid);
      expect(button('Schedule change').disabled).toBe(true);
    }
    typeLimit('92233720368.54775807');
    button('Schedule change').click();
    const req = http.expectOne({ method: 'PUT', url });
    expect(req.request.body).toEqual({ enabled: true, limit_usd: '92233720368.54775807' });
    req.flush({ ...status, scheduled: { ...status.scheduled!, limit_usd: '92233720368.54775807' } });
    await detect();
    expect(host.textContent).toContain('$92,233,720,368.54775807');
    expect(host.querySelector('[role="status"]')?.textContent).toContain('scheduled');
  });

  it('keeps the editor after a failed write and schedules removal without a limit', async () => {
    await load({ ...status, bound_violation: true });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('exceeded its reservation');
    button('Edit spend cap').click();
    fixture.detectChanges();
    host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    fixture.detectChanges();
    expect(host.querySelector('[inputmode="decimal"]')).toBeNull();
    button('Schedule change').click();
    const req = http.expectOne({ method: 'PUT', url });
    expect(req.request.body).toEqual({ enabled: false });
    req.flush(null, { status: 409, statusText: 'Conflict' });
    await detect();
    expect(host.textContent).toContain('Could not schedule');
    button('Schedule change').click();
    http.expectOne({ method: 'PUT', url }).flush({ ...status, scheduled: { ...status.scheduled!, enabled: false, limit_usd: null } });
    await detect();
    expect(host.textContent).toContain('Remove cap');
  });
});
