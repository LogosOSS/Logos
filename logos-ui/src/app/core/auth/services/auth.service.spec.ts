import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { KEYCLOAK } from '../keycloak';
import { User } from '../models/user.model';
import { AuthService } from './auth.service';

const pendingUser: User = {
  user_id: 1,
  username: 'developer',
  prename: 'Test',
  name: 'Developer',
  email: 'developer@example.com',
  role: 'app_developer',
  teams: [],
  logging_onboarding_teams: [{ id: 10, name: 'First team' }, { id: 20, name: 'Second team' }],
};

describe('AuthService', () => {
  const keycloak = { authenticated: false, token: 'old-token', updateToken: vi.fn() };
  let service: AuthService;
  let http: HttpTestingController;

  beforeEach(() => {
    keycloak.authenticated = false;
    keycloak.token = 'old-token';
    keycloak.updateToken.mockReset();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: KEYCLOAK, useValue: keycloak }],
    });
    service = TestBed.inject(AuthService);
    http = TestBed.inject(HttpTestingController);
    keycloak.authenticated = true;
  });

  afterEach(() => http.verify());

  it('forces Keycloak to refresh when the server reports expiry', async () => {
    keycloak.updateToken.mockImplementation(async () => { keycloak.token = 'fresh-token'; });
    expect(await service.freshToken(true)).toBe('fresh-token');
    expect(keycloak.updateToken).toHaveBeenCalledWith(-1);
  });

  it('checks the normal validity window for other connections', async () => {
    expect(await service.freshToken()).toBe('old-token');
    expect(keycloak.updateToken).toHaveBeenCalledWith(30);
  });

  it('does not return the old token when forced refresh fails', async () => {
    keycloak.updateToken.mockRejectedValue(new Error('unreachable'));
    expect(await service.freshToken(true)).toBe('');
  });

  it('clears only the team with a successful onboarding choice', async () => {
    service.currentUser.set(pendingUser);
    const result = service.completeLoggingOnboarding(10, true);
    const request = http.expectOne({ method: 'POST', url: '/api/teams/10/logging-onboarding' });
    expect(request.request.body).toEqual({ full_logging: true });
    request.flush(null, { status: 204, statusText: 'No Content' });

    expect(await result).toBe(true);
    expect(service.pendingLoggingTeams().map(team => team.id)).toEqual([20]);
  });

  it.each([true, false])('reports the saved choice (%s) on conflict', async (savedChoice) => {
    service.currentUser.set(pendingUser);
    const result = service.completeLoggingOnboarding(20, !savedChoice);
    http.expectOne('/api/teams/20/logging-onboarding').flush(
      { full_logging: savedChoice },
      { status: 409, statusText: 'Conflict' },
    );

    expect(await result).toBe(savedChoice);
    expect(service.pendingLoggingTeams().map(team => team.id)).toEqual([10]);
  });

  it.each([
    { status: 409, body: { full_logging: 'yes' } },
    { status: 503, body: {} },
  ])('keeps onboarding pending after a failed save ($status)', async ({ status, body }) => {
    service.currentUser.set(pendingUser);
    const result = service.completeLoggingOnboarding(10, true);
    http.expectOne('/api/teams/10/logging-onboarding').flush(body, { status, statusText: 'Save failed' });

    await expect(result).rejects.toMatchObject({ status });
    expect(service.pendingLoggingTeams().map(team => team.id)).toEqual([10, 20]);
  });

  it('leaves a different account pending when an earlier save finishes', async () => {
    service.currentUser.set(pendingUser);
    const result = service.completeLoggingOnboarding(10, true);
    const otherUser = { ...pendingUser, user_id: 2 };
    service.currentUser.set(otherUser);
    http.expectOne('/api/teams/10/logging-onboarding').flush(null, { status: 204, statusText: 'No Content' });

    await result;
    expect(service.currentUser()).toBe(otherUser);
  });
});
