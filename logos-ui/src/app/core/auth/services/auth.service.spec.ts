import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { KEYCLOAK } from '../keycloak';
import { AuthService } from './auth.service';

describe('AuthService token refresh', () => {
  const keycloak = { authenticated: false, token: 'old-token', updateToken: vi.fn() };
  let service: AuthService;

  beforeEach(() => {
    keycloak.authenticated = false;
    keycloak.token = 'old-token';
    keycloak.updateToken.mockReset();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), { provide: KEYCLOAK, useValue: keycloak }],
    });
    service = TestBed.inject(AuthService);
    keycloak.authenticated = true;
  });

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
});
