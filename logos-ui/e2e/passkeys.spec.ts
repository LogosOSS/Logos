import { expect, test, type Page } from '@playwright/test';

const PASSKEY_TYPE = 'webauthn-passwordless';
interface Credential { id: string; type: string; userLabel: string; createdDate: number; credentialData?: string; }

/** Account responses are fixtures; the browser WebAuthn ceremony uses a virtual authenticator. */
async function accountFixture(page: Page) {
  let credentials: Credential[] = [{ id: 'existing-passkey', type: PASSKEY_TYPE, userLabel: 'Personal laptop', createdDate: 1_700_000_000_000 }];
  const deleted: string[] = [];
  const saved: Record<string, string>[] = [];
  let failList = false;
  let failDelete = false;
  const corsHeaders = () => ({
    'access-control-allow-origin': new URL(page.url()).origin,
    'access-control-allow-credentials': 'true',
  });

  await page.route('**/api/info', async route => {
    const response = await route.fetch();
    const json = await response.json();
    json.keycloak.passkey_rp_id = 'localhost';
    await route.fulfill({ response, json });
  });
  await page.route(/\/realms\/[^/]+\/account\/credentials(?:\/[^?]+)?(?:\?.*)?$/, async route => {
    const request = route.request();
    expect(request.headers()['authorization']).toMatch(/^Bearer .+/);
    if (request.method() === 'DELETE') {
      if (failDelete) return route.fulfill({ status: 403, headers: corsHeaders() });
      const id = decodeURIComponent(new URL(request.url()).pathname.split('/').pop()!);
      deleted.push(id);
      credentials = credentials.filter(credential => credential.id !== id);
      return route.fulfill({ status: 204, headers: corsHeaders() });
    }
    if (failList) return route.fulfill({ status: 403, headers: corsHeaders() });
    return route.fulfill({ headers: corsHeaders(), json: [
      { type: 'password', removeable: true, userCredentialMetadatas: [{ credential: { id: 'password', type: 'password', userLabel: 'Password' } }] },
      { type: 'webauthn', removeable: true, userCredentialMetadatas: [{ credential: { id: 'otp-key', type: 'webauthn', userLabel: 'Second factor' } }] },
      { type: PASSKEY_TYPE, removeable: true, userCredentialMetadatas: credentials.map(credential => ({ credential })) },
    ] });
  });
  await page.route('**/passkey/*/challenge', route => route.fulfill({
    headers: corsHeaders(), json: { challenge: Buffer.alloc(32, 7).toString('base64url') },
  }));
  await page.route('**/passkey/*/save', async route => {
    const payload = route.request().postDataJSON();
    expect(route.request().headers()['authorization']).toMatch(/^Bearer .+/);
    expect(payload.attestationObject).toBeTruthy();
    expect(payload.clientDataJSON).toBeTruthy();
    saved.push(payload);
    credentials.push({
      id: `added-${saved.length}`, type: PASSKEY_TYPE, userLabel: payload.deviceName, createdDate: Date.now(),
      credentialData: JSON.stringify({ credentialId: payload.credentialId }),
    });
    await route.fulfill({ status: 200, headers: corsHeaders(), json: { status: 'ok' } });
  });
  return {
    deleted, saved,
    failList: (value: boolean) => { failList = value; },
    failDelete: (value: boolean) => { failDelete = value; },
    empty: () => { credentials = []; },
  };
}

test('account menu opens passkeys, adds with WebAuthn and confirms account-wide removal', async ({ page, context }) => {
  const fixture = await accountFixture(page);
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  await cdp.send('WebAuthn.addVirtualAuthenticator', { options: {
    protocol: 'ctap2', transport: 'usb', hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true,
  } });
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Account settings' })).toBeVisible();
  await page.getByRole('button', { name: /Open account menu for/ }).click();
  await page.getByRole('link', { name: 'Account settings' }).press('Enter');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Personal laptop' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Password', exact: true })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Second factor', exact: true })).toHaveCount(0);

  await page.getByRole('button', { name: 'Add passkey', exact: true }).click();
  const addDialog = page.getByRole('dialog', { name: 'Add passkey' });
  await addDialog.getByLabel('Passkey name').fill('Backup security key');
  await addDialog.getByLabel('Passkey name').press('Enter');
  await expect(addDialog).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Backup security key' })).toBeVisible();
  expect(fixture.saved).toHaveLength(1);

  await page.getByRole('button', { name: 'Remove Personal laptop', exact: true }).click();
  const removeDialog = page.getByRole('dialog', { name: 'Remove passkey?' });
  await expect(removeDialog).toContainText('any other application using this account');
  await removeDialog.getByRole('button', { name: 'Cancel' }).click();
  expect(fixture.deleted).toEqual([]);
  await page.getByRole('button', { name: 'Remove Personal laptop', exact: true }).click();
  await removeDialog.getByRole('button', { name: 'Remove passkey', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Personal laptop' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Backup security key' })).toBeVisible();
  expect(fixture.deleted).toEqual(['existing-passkey']);
  await cdp.send('WebAuthn.disable');
});

test('mobile settings recover from account errors and preserve a passkey when deletion is denied', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  const fixture = await accountFixture(page);
  fixture.failList(true);
  await page.goto('/settings');
  await expect(page.getByRole('alert')).toContainText('account did not allow');
  await expect(page.getByRole('heading', { name: 'No passkeys yet' })).toHaveCount(0);
  fixture.failList(false);
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('heading', { name: 'Personal laptop' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

  fixture.failDelete(true);
  await page.getByRole('button', { name: 'Remove Personal laptop' }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove passkey?' });
  await dialog.getByRole('button', { name: 'Remove passkey', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('account did not allow');
  expect(fixture.deleted).toEqual([]);
  await dialog.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Remove Personal laptop' })).toBeFocused();
  fixture.empty();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'No passkeys yet' })).toBeVisible();
});
