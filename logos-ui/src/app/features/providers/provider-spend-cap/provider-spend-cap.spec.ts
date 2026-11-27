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

  it('shows the current limit, settled charges and upcoming change without exposing reservations', async () => {
    expect(host.textContent).toContain('Loading spend cap');
    await load();
    for (const text of ['$400.00', '$87.25', 'December 1, 2026 (UTC)', '$500.00 limit', 'No deployments have a verified cost bound']) {
      expect(host.textContent).toContain(text);
    }
    expect(host.textContent).not.toContain('Reserved this month');
    expect(host.textContent).not.toContain('$10.50');
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

  it('edits the current value, rejects excess precision and sends an exact decimal string', async () => {
    await load();
    button('Edit spend cap').click();
    fixture.detectChanges();
    expect(host.querySelector<HTMLInputElement>('[inputmode="decimal"]')?.value).toBe('400');
    for (const invalid of ['-1', '1.000000001', '9e9', '92233720368.54775808']) {
      typeLimit(invalid);
      expect(button('Save changes').disabled).toBe(true);
    }
    typeLimit('92233720368.54775807');
    button('Save changes').click();
    const req = http.expectOne({ method: 'PUT', url });
    expect(req.request.body).toEqual({ enabled: true, limit_usd: '92233720368.54775807' });
    req.flush({ ...status, current: { ...status.current!, limit_usd: '92233720368.54775807' } });
    await detect();
    expect(host.textContent).toContain('$92,233,720,368.54775807');
    expect(host.textContent).toContain('$500.00 limit');
    expect(host.textContent).toContain('$87.25');
    expect(host.querySelector('[role="status"]')?.textContent).toContain('Spend cap updated');
  });

  it('keeps the editor after a failed write and removes the current limit without changing a scheduled override', async () => {
    await load({ ...status, bound_violation: true });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain('exceeded the verified bound');
    button('Edit spend cap').click();
    fixture.detectChanges();
    host.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click();
    fixture.detectChanges();
    expect(host.querySelector('[inputmode="decimal"]')).toBeNull();
    button('Save changes').click();
    const req = http.expectOne({ method: 'PUT', url });
    expect(req.request.body).toEqual({ enabled: false });
    req.flush(null, { status: 409, statusText: 'Conflict' });
    await detect();
    expect(host.textContent).toContain('Could not save');
    button('Save changes').click();
    http.expectOne({ method: 'PUT', url }).flush({ ...status, current: { ...status.current!, enabled: false, limit_usd: null } });
    await detect();
    expect(host.textContent).toContain('No cap');
    expect(host.textContent).toContain('$500.00 limit');
    button('Edit spend cap').click();
    fixture.detectChanges();
    expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(false);
  });

  it('formats numeric and exact string amounts without changing a cancelled policy', async () => {
    const page = fixture.componentInstance;
    expect(page.usdLabel(null)).toBe('Unavailable');
    expect(page.usdLabel('0.00000000')).toBe('$0.00');
    expect(page.usdLabel('10000')).toBe('$10,000.00');
    expect(page.usdLabel('999999.10101000')).toBe('$999,999.10101');
    expect(page.usdLabel(1234567.25)).toBe('$1,234,567.25');
    await page.load(); // A pending load does not start a second request.
    await load({ ...status, current: { ...status.current!, limit_usd: 1234567.25 }, scheduled: null, bounded_deployments: 1 });
    expect(host.textContent).toContain('Verified cost bounds: 1 deployment.');
    button('Edit spend cap').click();
    fixture.detectChanges();
    expect(host.querySelector<HTMLInputElement>('[inputmode="decimal"]')?.value).toBe('1234567.25');
    typeLimit('500');
    button('Cancel').click();
    await page.save(); // Cannot write outside the editor.
    await detect();
    expect(host.textContent).toContain('$1,234,567.25');
    expect(host.querySelector('[inputmode="decimal"]')).toBeNull();
  });

  it('keeps a pending write protected from cancel or duplicate submits', async () => {
    await load({ ...status, current: null, scheduled: null, bounded_deployments: 2 });
    button('Edit spend cap').click();
    fixture.detectChanges();
    expect(host.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).toBe(true);
    expect(host.querySelector<HTMLInputElement>('[inputmode="decimal"]')?.value).toBe('');
    await fixture.componentInstance.save();
    typeLimit('0');
    button('Save changes').click();
    const req = http.expectOne({ method: 'PUT', url });
    expect(req.request.body).toEqual({ enabled: true, limit_usd: '0' });
    fixture.componentInstance.cancel();
    await fixture.componentInstance.save();
    expect(fixture.componentInstance.editing()).toBe(true);
    req.flush({ ...status, current: { ...status.current!, limit_usd: '0' }, scheduled: null });
    await detect();
    expect(host.textContent).toContain('$0.00');
  });

});
