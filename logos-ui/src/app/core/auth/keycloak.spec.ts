import { beforeEach, describe, expect, it, vi } from 'vitest';

const mock = vi.hoisted(() => ({ init: vi.fn(), updateToken: vi.fn(), instances: [] as any[] }));
vi.mock('keycloak-js', () => ({ default: class {
  init = mock.init;
  updateToken = mock.updateToken;
  onTokenExpired?: () => void;
  constructor(readonly options: unknown) { mock.instances.push(this); }
} }));

beforeEach(() => {
  vi.resetModules();
  mock.init.mockReset().mockResolvedValue(true);
  mock.updateToken.mockReset().mockResolvedValue(true);
  mock.instances.length = 0;
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    keycloak: { issuer: 'https://identity.example/realms/tum', client_id: 'logos' },
  }) }));
});

describe('Keycloak startup', () => {
  it('uses a redirect SSO check without a third-party iframe and keeps PKCE enabled', async () => {
    const { initKeycloak, getKeycloak } = await import('./keycloak');
    const keycloak = await initKeycloak();
    expect(mock.instances[0].options).toEqual({ url: 'https://identity.example', realm: 'tum', clientId: 'logos' });
    expect(mock.init).toHaveBeenCalledExactlyOnceWith({ onLoad: 'check-sso', pkceMethod: 'S256', checkLoginIframe: false });
    expect(getKeycloak()).toBe(keycloak);
    mock.instances[0].onTokenExpired();
    expect(mock.updateToken).toHaveBeenCalledWith(30);
  });

  it('does not continue after an identity service initialization failure', async () => {
    mock.init.mockRejectedValue(new Error('Identity service unavailable'));
    const { initKeycloak } = await import('./keycloak');
    await expect(initKeycloak()).rejects.toThrow('Identity service unavailable');
  });
});
