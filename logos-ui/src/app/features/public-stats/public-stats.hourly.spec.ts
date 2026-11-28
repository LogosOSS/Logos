import { TestBed } from '@angular/core/testing';
import { PublicStatsHourly } from './public-stats.hourly';

describe('Public local request arrivals', () => {
  it('shows exact hourly totals and includes Agent and benchmarks independently', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    fixture.componentRef.setInput('load', {
      hours: [
        { start: '2026-10-25T00:00:00Z', regular: 1, agent: 2, benchmark: 4 },
        { start: '2026-10-25T01:00:00Z', regular: 0, agent: 0, benchmark: 0 },
      ],
    });
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    const slots = [...root.querySelectorAll<HTMLButtonElement>('.hourly-public-hour')];
    const [agent, benchmark] = [...root.querySelectorAll<HTMLInputElement>('input[type=checkbox]')];
    expect(slots[0].getAttribute('aria-label')).toContain('02:00 GMT+2: 1 requests');
    expect(slots[1].getAttribute('aria-label')).toContain('02:00 GMT+1: 0 requests');
    expect(slots[1].classList.contains('quiet')).toBe(true);
    slots[0].click();
    const output = () => {
      fixture.detectChanges();
      return root.querySelector('output')?.textContent;
    };
    expect(output()).toContain('1 requests');
    agent.click();
    expect(output()).toContain('3 requests');
    benchmark.click();
    expect(output()).toContain('7 requests');
    agent.click();
    expect(output()).toContain('5 requests');
  });

  it('paginates actual hours without grouping and starts with the newest hours', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    const start = Date.parse('2026-11-22T00:00:00Z');
    fixture.componentRef.setInput('load', {
      hours: Array.from({ length: 48 }, (_, i) => ({
        start: new Date(start + 3_600_000 * i).toISOString(), regular: i, agent: 0, benchmark: 0,
      })),
    });
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.querySelectorAll('.hourly-public-hour')).toHaveLength(24);
    const first = () => root.querySelector('.hourly-public-hour')?.getAttribute('aria-label');
    expect(first()).toContain('24 requests');
    root.querySelector<HTMLButtonElement>('.hourly-public-navigation button')!.click();
    fixture.detectChanges();
    expect(root.querySelectorAll('.hourly-public-hour')).toHaveLength(24);
    expect(first()).toContain('0 requests');
  });

  it('reports missing data without drawing zeros', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.textContent).toContain('currently unavailable');
    expect(root.querySelectorAll('.hourly-public-hour')).toHaveLength(0);
  });
});
