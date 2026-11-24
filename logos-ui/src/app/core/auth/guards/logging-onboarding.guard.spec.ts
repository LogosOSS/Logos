import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { User } from '../models/user.model';
import { AuthService } from '../services/auth.service';
import { hasLoggingOnboarding, requireLoggingOnboarding } from './logging-onboarding.guard';

@Component({ template: '' })
class Stub {}

const developer: User = {
  user_id: 1,
  username: 'developer',
  prename: 'Test',
  name: 'Developer',
  email: 'developer@example.com',
  role: 'app_developer',
  teams: [],
};

describe('Logging onboarding guards', () => {
  let router: Router;
  const currentUser = signal<User | null>(null);

  beforeEach(() => {
    currentUser.set(developer);
    TestBed.configureTestingModule({
      providers: [
        { provide: AuthService, useValue: { currentUser } },
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
    currentUser.set({ ...developer, logging_onboarding_pending: true });
    const destination = '/my-workspace?view=keys#logging';

    await router.navigateByUrl(destination);

    expect(router.url).toMatch(/^\/onboarding\?/);
    expect(router.parseUrl(router.url).queryParams['returnUrl']).toBe(destination);
  });

  it.each([false, undefined])('opens the workspace directly when the pending flag is %s', async (pending) => {
    currentUser.set({ ...developer, logging_onboarding_pending: pending });

    await router.navigateByUrl('/my-workspace');

    expect(router.url).toBe('/my-workspace');
  });

  it('redirects a user who already completed onboarding away from the choice page', async () => {
    currentUser.set({ ...developer, logging_onboarding_pending: false });

    await router.navigateByUrl('/onboarding');

    expect(router.url).toBe('/');
  });
});
