import { TestBed } from '@angular/core/testing';
import { Subject } from 'rxjs';
import { StatisticsService, type HourlyRequestLoad } from '../../services/statistics.service';
import { HourlyRequestLoadComponent } from './hourly-request-load';

const scope = { teamId: 1, userId: null, providerId: null, apiKeyId: null, modelIds: [3] };
const slots = Array.from({ length: 24 }, (_, hour) => ({
  hour,
  complete_hours: hour >= 8 && hour <= 10 ? 2 : 0,
  other_requests: hour === 9 ? 5 : 0,
  agent_requests: hour === 9 ? 4 : 0,
  benchmark_requests: hour === 9 ? 16 : 0,
}));
const load: HourlyRequestLoad = {
  time_zone: 'Europe/Berlin',
  all_days: slots,
  weekdays: slots,
  weekends: slots.map(slot => ({ ...slot, complete_hours: 0 })),
};

describe('Hourly request load', () => {
  it('renders completed-slot means, labels zero and absent hours differently, and switches day type', async () => {
    const response = new Subject<HourlyRequestLoad>();
    const getHourlyRequestLoad = vi.fn(() => response);
    await TestBed.configureTestingModule({
      imports: [HourlyRequestLoadComponent],
      providers: [{ provide: StatisticsService, useValue: { getHourlyRequestLoad } }],
    }).compileComponents();
    const fixture = TestBed.createComponent(HourlyRequestLoadComponent);
    fixture.componentRef.setInput('start', '2026-11-24T00:00:00Z');
    fixture.componentRef.setInput('end', '2026-11-26T00:00:00Z');
    fixture.componentRef.setInput('scope', scope);
    fixture.detectChanges();
    expect(getHourlyRequestLoad).toHaveBeenCalledWith('2026-11-24T00:00:00Z', '2026-11-26T00:00:00Z', scope);
    response.next(load);
    fixture.detectChanges();

    const root: HTMLElement = fixture.nativeElement;
    const bars = root.querySelectorAll<HTMLButtonElement>('button.hourly-slot');
    expect(bars).toHaveLength(24);
    expect(bars[7].disabled).toBe(true);
    expect(bars[7].getAttribute('aria-label')).toContain('no complete hours');
    expect(bars[8].disabled).toBe(false);
    expect(bars[8].getAttribute('aria-label')).toContain('0 average logged requests');
    expect(bars[9].getAttribute('aria-label')).toContain('4.5 average logged requests');
    expect(root.querySelector('.hourly-details')?.textContent).toContain('Other requests: 2.5');
    expect(root.querySelector('.hourly-timezone')?.textContent).toBe('Europe/Berlin');
    const [agent, benchmarks] = root.querySelectorAll<HTMLInputElement>('input[type=checkbox]');
    expect(agent.checked).toBe(true);
    expect(benchmarks.checked).toBe(false);
    benchmarks.click();
    fixture.detectChanges();
    expect(bars[9].getAttribute('aria-label')).toContain('12.5 average logged requests');
    agent.click();
    fixture.detectChanges();
    expect(benchmarks.checked).toBe(true);
    expect(bars[9].getAttribute('aria-label')).toContain('10.5 average logged requests');
    benchmarks.click();
    fixture.detectChanges();
    expect(agent.checked).toBe(false);
    expect(bars[9].getAttribute('aria-label')).toContain('2.5 average logged requests');
    bars[8].click();
    fixture.detectChanges();
    expect(root.querySelector('.hourly-details')?.textContent).toContain('08:00–09:00');

    root.querySelectorAll<HTMLButtonElement>('.hourly-groups button')[2].click();
    fixture.detectChanges();
    expect(root.textContent).toContain('No complete hours');
    expect(getHourlyRequestLoad).toHaveBeenCalledTimes(1);
  });

  it('ignores cancelled responses when the page scope changes', async () => {
    const old = new Subject<HourlyRequestLoad>();
    const current = new Subject<HourlyRequestLoad>();
    const getHourlyRequestLoad = vi.fn().mockReturnValueOnce(old).mockReturnValueOnce(current);
    await TestBed.configureTestingModule({
      imports: [HourlyRequestLoadComponent],
      providers: [{ provide: StatisticsService, useValue: { getHourlyRequestLoad } }],
    }).compileComponents();
    const fixture = TestBed.createComponent(HourlyRequestLoadComponent);
    fixture.componentRef.setInput('start', '2026-11-24T00:00:00Z');
    fixture.componentRef.setInput('end', '2026-11-26T00:00:00Z');
    fixture.componentRef.setInput('scope', scope);
    fixture.detectChanges();
    fixture.componentRef.setInput('scope', { ...scope, teamId: 2 });
    fixture.detectChanges();
    old.next(load);
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.querySelector('.hourly-details')).toBeNull();
    current.next(load);
    fixture.detectChanges();
    expect(root.querySelector('.hourly-details')).not.toBeNull();
  });
});
