import { TestBed } from '@angular/core/testing';
import { PublicStatsHourly } from './public-stats.hourly';

describe('Public hourly load', () => {
  it('distinguishes low volume, withheld and actual zero without exposing exact counts', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    fixture.componentRef.setInput('load', {
      time_zone: 'Europe/Berlin', from: '2026-10-26', until: '2026-11-23', band_size: 5,
      hours: [
        { hour: 0, band_start: 0, zero: false }, { hour: 1, band_start: null, zero: false },
        { hour: 2, band_start: null, zero: true }, { hour: 3, band_start: 20, zero: false },
      ],
    });
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.querySelector('select')).toBeNull();
    const slots = [...root.querySelectorAll<HTMLButtonElement>('.hourly-public-hour')];
    expect(slots[0].getAttribute('aria-label')).toContain('Under 5 requests per hour');
    expect(slots[1].classList.contains('suppressed')).toBe(true);
    expect(slots[2].classList.contains('quiet')).toBe(true);
    slots[3].click();
    fixture.detectChanges();
    expect(root.querySelector('output')?.textContent).toContain('20 to <25 requests per hour');
  });

  it('labels a grouped band with its full time range and spans those columns', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    fixture.componentRef.setInput('load', {
      time_zone: 'Europe/Berlin', from: '2026-10-26', until: '2026-11-23', band_size: 5,
      hours: [{ hour: 20, span: 4, band_start: 0, zero: false }],
    });
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.querySelector<HTMLElement>('.hourly-public-column')?.style.gridColumn).toBe('21 / span 4');
    const button = root.querySelector('button')!;
    expect(button.getAttribute('aria-label')).toBe('20:00 to 00:00: Under 5 requests per hour');
    button.click();
    fixture.detectChanges();
    expect(root.querySelector('output')?.textContent).toContain('20:00–00:00');
  });

  it('does not invent a chart without a publishable cohort', () => {
    const fixture = TestBed.createComponent(PublicStatsHourly);
    fixture.detectChanges();
    const root: HTMLElement = fixture.nativeElement;
    expect(root.textContent).toContain('No hourly figures');
    expect(root.querySelectorAll('.hourly-public-hour')).toHaveLength(0);
  });
});
