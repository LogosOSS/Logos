import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { AuthService } from './core/auth/services/auth.service';
import { provideRouter, Router } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { routes } from './app.routes';
import { ThemeService } from './core/services/theme.service';
import { PublicStats } from './features/public-stats/public-stats';
import { PublicStatsService } from './features/public-stats/public-stats.service';

describe('public usage routes', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes),
        { provide: AuthService, useValue: { status: signal('unauthenticated') } },
        { provide: ThemeService, useValue: { isDark: signal(false) } },
        { provide: PublicStatsService, useValue: { getStats: async () => new Promise(() => {}) } },
      ],
    });
  });

  it.each([
    ['/usage', '/usage'],
    ['/usage?source=docs#growth-title', '/usage?source=docs#growth-title'],
  ])('opens the usage page without signing in at %s', async (url, expected) => {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl(url, PublicStats);

    expect(TestBed.inject(Router).url).toBe(expected);
    expect(document.title).toBe('Usage · Logos');
    expect(harness.routeNativeElement?.querySelector('button.btn-secondary')?.textContent).toContain('Sign in');
  });

  it.each(['/stats', '/stats?source=docs#growth-title'])('does not serve public usage at %s', async (url) => {
    const harness = await RouterTestingHarness.create();
    const component = await harness.navigateByUrl(url);

    expect(component).not.toBeInstanceOf(PublicStats);
    expect(TestBed.inject(Router).url).toBe('/');
  });
});
