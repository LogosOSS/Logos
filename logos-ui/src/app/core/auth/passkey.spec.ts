import { registerPasskey } from './passkey';

const tokenFor = (subject: string) => `header.${btoa(JSON.stringify({ sub: subject }))}.signature`;

describe('passkey registration', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;
  let create: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    class Attestation {
      clientDataJSON = new Uint8Array([1]).buffer;
      attestationObject = new Uint8Array([2]).buffer;
    }
    class Credential {
      rawId = new Uint8Array([3]).buffer;
      response = new Attestation();
    }
    create = vi.fn().mockResolvedValue(new Credential());
    vi.stubGlobal('isSecureContext', true);
    vi.stubGlobal('PublicKeyCredential', Credential);
    vi.stubGlobal('AuthenticatorAttestationResponse', Attestation);
    vi.stubGlobal('navigator', { credentials: { create }, userAgent: 'Mac Chrome' });
    fetchSpy = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ challenge: 'Y2hhbGxlbmdl' })))
      .mockResolvedValue(new Response(null, { status: 201 }));
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => vi.unstubAllGlobals());

  it('keeps the default device name and token for callers without management options', async () => {
    await registerPasskey('https://identity.example.test/realms/test', 'logos', tokenFor('user-1'), 'localhost');
    const save = fetchSpy.mock.calls[1][1];
    expect(save.headers.Authorization).toBe(`Bearer ${tokenFor('user-1')}`);
    expect(JSON.parse(save.body).deviceName).toBe('Mac - Chrome');
    expect(JSON.parse(save.body)).not.toHaveProperty('label');
  });

  it('never saves a newly created credential if the session changed account', async () => {
    await expect(registerPasskey('https://identity.example.test/realms/test', 'logos', tokenFor('user-1'), 'localhost', 'Logos', {
      freshToken: async () => tokenFor('user-2'),
    })).rejects.toThrow('account changed');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('explains challenge network and CORS failures before opening WebAuthn', async () => {
    fetchSpy.mockReset().mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(registerPasskey('https://identity.example.test/realms/test', 'logos', tokenFor('user-1')))
      .rejects.toThrow('passkey service could not be reached');
    expect(create).not.toHaveBeenCalled();
  });

  it('explains save network and CORS failures after the browser ceremony', async () => {
    fetchSpy.mockReset()
      .mockResolvedValueOnce(new Response(JSON.stringify({ challenge: 'Y2hhbGxlbmdl' })))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'));
    await expect(registerPasskey('https://identity.example.test/realms/test', 'logos', tokenFor('user-1')))
      .rejects.toThrow('passkey service could not be reached');
    expect(create).toHaveBeenCalledTimes(1);
  });
});
