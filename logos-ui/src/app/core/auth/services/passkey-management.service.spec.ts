import { TestBed } from '@angular/core/testing';
import { AuthService } from './auth.service';
import { accountPasskeys, PasskeyManagementService } from './passkey-management.service';

const credential = (id = 'passkey-1', type = 'webauthn-passwordless') => ({
  credential: { id, type, userLabel: 'Laptop', createdDate: 1_700_000_000_000, credentialData: '{"credentialId":"a2V5"}' },
});
const responseBody = () => [
  { type: 'password', removeable: true, userCredentialMetadatas: [credential('password', 'password')] },
  { type: 'webauthn', removeable: true, userCredentialMetadatas: [credential('second-factor', 'webauthn')] },
  { type: 'webauthn-passwordless', removeable: true, userCredentialMetadatas: [credential()] },
];

describe('accountPasskeys', () => {
  it('lists passwordless credentials and no other authentication methods', () => {
    expect(accountPasskeys(responseBody())).toEqual([{
      id: 'passkey-1', label: 'Laptop', createdAt: 1_700_000_000_000, removable: true, credentialId: 'a2V5',
    }]);
  });

  it('requires both the container and individual credential to have the passkey type', () => {
    const body = responseBody();
    body[2].userCredentialMetadatas.push(credential('password-in-wrong-container', 'password'));
    expect(accountPasskeys(body).map(passkey => passkey.id)).toEqual(['passkey-1']);
  });

  it('handles unnamed credentials without optional metadata and respects removal policy', () => {
    expect(accountPasskeys([{
      type: 'webauthn-passwordless', removeable: false,
      userCredentialMetadatas: [{ credential: { id: 'key', type: 'webauthn-passwordless', credentialData: 'invalid' } }],
    }])).toEqual([{ id: 'key', label: 'Unnamed passkey', createdAt: null, removable: false, credentialId: undefined }]);
  });

  it('distinguishes a configured account with no passkeys from a disabled passkey type', () => {
    expect(accountPasskeys([{ type: 'webauthn-passwordless', userCredentialMetadatas: [] }])).toEqual([]);
    expect(() => accountPasskeys([])).toThrow('not enabled');
    expect(() => accountPasskeys({})).toThrow('unexpected response');
    expect(() => accountPasskeys([{ type: 'webauthn-passwordless' }])).toThrow('did not return');
  });
});

describe('PasskeyManagementService', () => {
  const freshToken = vi.fn();
  let fetchSpy: ReturnType<typeof vi.fn>;
  let service: PasskeyManagementService;

  beforeEach(() => {
    vi.clearAllMocks();
    freshToken.mockResolvedValue('fresh-token');
    fetchSpy = vi.fn().mockImplementation(async () => new Response(JSON.stringify(responseBody())));
    vi.stubGlobal('fetch', fetchSpy);
    TestBed.configureTestingModule({ providers: [{ provide: AuthService, useValue: {
      freshToken, keycloak: { authServerUrl: 'https://identity.example.test/', realm: 'test', clientId: 'logos' },
    } }] });
    service = TestBed.inject(PasskeyManagementService);
  });

  afterEach(() => vi.unstubAllGlobals());

  it('sends only a fresh user token to the account endpoint', async () => {
    await service.list();
    expect(freshToken).toHaveBeenCalledWith(false);
    expect(fetchSpy).toHaveBeenCalledWith('https://identity.example.test/realms/test/account/credentials?type=webauthn-passwordless', {
      method: 'GET', headers: { Authorization: 'Bearer fresh-token', Accept: 'application/json' },
      credentials: 'omit', cache: 'no-store', redirect: 'error',
    });
  });

  it('refreshes once on 401 before retrying the request', async () => {
    freshToken.mockResolvedValueOnce('expired-token').mockResolvedValueOnce('renewed-token');
    fetchSpy.mockResolvedValueOnce(new Response('', { status: 401 }));
    await service.list();
    expect(freshToken.mock.calls).toEqual([[false], [true]]);
    expect(fetchSpy.mock.calls[1][1].headers.Authorization).toBe('Bearer renewed-token');
  });

  it('stops after a second 401 and explains that sign-in is required', async () => {
    fetchSpy.mockImplementation(async () => new Response('', { status: 401 }));
    await expect(service.list()).rejects.toThrow('session has expired');
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it('does not send a request if token refresh failed', async () => {
    freshToken.mockResolvedValue('');
    await expect(service.list()).rejects.toThrow('session has expired');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('reports account permissions and network failures without an empty list', async () => {
    fetchSpy.mockResolvedValueOnce(new Response('', { status: 403 }));
    await expect(service.list()).rejects.toThrow('account did not allow');
    fetchSpy.mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(service.list()).rejects.toThrow('could not be reached');
    fetchSpy.mockResolvedValueOnce(new Response('<html>Sign in</html>'));
    await expect(service.list()).rejects.toThrow('unexpected response');
  });

  it('rechecks the requested credential and URL-encodes its id before deletion', async () => {
    const body = responseBody();
    body[2].userCredentialMetadatas[0].credential.id = 'passkey/id';
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify(body)));
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await service.remove('passkey/id');
    expect(fetchSpy.mock.calls[1][0]).toBe('https://identity.example.test/realms/test/account/credentials/passkey%2Fid');
    expect(fetchSpy.mock.calls[1][1].method).toBe('DELETE');
    expect(freshToken).toHaveBeenCalledTimes(2);
  });

  it('refuses to delete passwords, second factors, unknown ids or policy-protected passkeys', async () => {
    for (const id of ['password', 'second-factor', 'missing']) {
      await expect(service.remove(id)).rejects.toThrow('no longer available');
    }
    const body = responseBody();
    body[2].removeable = false;
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify(body)));
    await expect(service.remove('passkey-1')).rejects.toThrow('no longer available');
    expect(fetchSpy.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
  });

  it('reuses registration with a chosen label, excluded existing keys and fresh save token', async () => {
    const token = (name: string) => `header.${btoa(JSON.stringify({ sub: 'user-1', name }))}.signature`;
    class Attestation {
      clientDataJSON = new Uint8Array([1]).buffer;
      attestationObject = new Uint8Array([2]).buffer;
    }
    class Credential {
      rawId = new Uint8Array([3]).buffer;
      response = new Attestation();
    }
    const create = vi.fn().mockResolvedValue(new Credential());
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('PublicKeyCredential', Credential);
    vi.stubGlobal('AuthenticatorAttestationResponse', Attestation);
    vi.stubGlobal('navigator', { credentials: { create }, userAgent: 'Mac Chrome' });
    freshToken.mockResolvedValueOnce(token('list')).mockResolvedValueOnce(token('create')).mockResolvedValueOnce(token('save'));
    fetchSpy.mockResolvedValueOnce(new Response(JSON.stringify(responseBody())))
      .mockResolvedValueOnce(new Response(JSON.stringify({ challenge: 'Y2hhbGxlbmdl' })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    await service.add('  Backup key  ');
    expect(create.mock.calls[0][0].publicKey.excludeCredentials).toEqual([
      { type: 'public-key', id: new Uint8Array([107, 101, 121]) },
    ]);
    expect(fetchSpy.mock.calls[2][0]).toBe('https://identity.example.test/realms/test/passkey/logos/save');
    expect(fetchSpy.mock.calls[2][1].headers.Authorization).toBe(`Bearer ${token('save')}`);
    expect(JSON.parse(fetchSpy.mock.calls[2][1].body).deviceName).toBe('Backup key');
  });

  it('rejects blank, oversized and duplicate labels before creating a credential', async () => {
    for (const label of [' ', 'a'.repeat(65), 'Laptop']) {
      await expect(service.add(label)).rejects.toThrow();
    }
    expect(fetchSpy.mock.calls.every(([url]) => url.includes('/account/credentials'))).toBe(true);
  });
});
