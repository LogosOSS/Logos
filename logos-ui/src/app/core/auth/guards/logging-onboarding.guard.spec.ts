import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { Team } from '../models/user.model';
import { AuthService } from '../services/auth.service';
import { hasLoggingOnboarding, requireLoggingOnboarding } from './logging-onboarding.guard';

@Component({ template: '' })
class Stub {}

describe('Logging onboarding guards', () => {
  let router: Router;
  const pendingLoggingTeams = signal<Team[]>([]);

  beforeEach(() => {
    pendingLoggingTeams.set([]);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { pendingLoggingTeams } },
        provideRouter([
          { path: '', pathMatch: 'full', component: Stub },
          { path: 'onboarding', canActivate: [hasLoggingOnboarding], component: Stub },
          {
            path: '',
            canActivateChild: [requireLoggingOnboarding],
            children: [{ path: 'my-workspace', component: Stub }],
          },
        ]),
      ],
    });
    router = TestBed.inject(Router);
  });

  it('preserves the destination when it sends a user to onboarding', async () => {
    pendingLoggingTeams.set([{ id: 10, name: 'First team' }]);
    const destination = '/my-workspace?view=keys#logging';

    await router.navigateByUrl(destination);

    expect(router.url).toMatch(/^\/onboarding\?/);
    expect(router.parseUrl(router.url).queryParams['returnUrl']).toBe(destination);
  });

  it('opens the workspace directly when no teams are pending', async () => {
    await router.navigateByUrl('/my-workspace');

    expect(router.url).toBe('/my-workspace');
  });

  it('redirects a user who already completed onboarding away from the choice page', async () => {
    await router.navigateByUrl('/onboarding');

    expect(router.url).toBe('/');
  });
});
