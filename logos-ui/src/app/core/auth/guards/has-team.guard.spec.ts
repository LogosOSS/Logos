import { Component, computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { RouterTestingModule } from '@angular/router/testing';

import { AuthService } from '../services/auth.service';
import { User, UserRole } from '../models/user.model';
import { hasTeamGuard } from './has-team.guard';

/**
 * The guard is the route-level boundary behind the shell's Models entry, so
 * it must let exactly the same callers through: logos_admin and app_admin
 * always, everyone else only with a team.
 */

@Component({ template: '' })
class Stub {}

const makeUser = (role: UserRole, teams: User['teams']): User => ({
  user_id: 1,
  username: 'operator',
  prename: 'Op',
  name: 'Erator',
  email: 'operator@example.com',
  role,
  teams,
});

const TEAM = [{ id: 1, name: 'Test team' }];

class FakeAuthService {
  currentUser = signal<User | null>(null);
  role = computed(() => this.currentUser()?.role ?? null);
  logout = () => {};
}

describe('hasTeamGuard', () => {
  let auth: FakeAuthService;
  let router: Router;

  beforeEach(async () => {
    auth = new FakeAuthService();
    await TestBed.configureTestingModule({
      imports: [
        RouterTestingModule.withRoutes([
          { path: 'models', component: Stub, canActivate: [hasTeamGuard] },
          { path: 'no-access', component: Stub },
        ]),
      ],
      providers: [{ provide: AuthService, useValue: auth }],
    }).compileComponents();

    router = TestBed.inject(Router);
  });

  const navigateToModels = async (): Promise<void> => {
    await router.navigate(['/models']);
  };

  it('lets a teamless logos admin through', async () => {
    auth.currentUser.set(makeUser('logos_admin', []));
    await navigateToModels();
    expect(router.url).toBe('/models');
  });

  it('lets a teamless app admin through', async () => {
    auth.currentUser.set(makeUser('app_admin', []));
    await navigateToModels();
    expect(router.url).toBe('/models');
  });

  it('lets a team member through', async () => {
    auth.currentUser.set(makeUser('app_developer', TEAM));
    await navigateToModels();
    expect(router.url).toBe('/models');
  });

  it('sends a teamless app developer to /no-access', async () => {
    auth.currentUser.set(makeUser('app_developer', []));
    await navigateToModels();
    expect(router.url).toBe('/no-access');
  });
});
