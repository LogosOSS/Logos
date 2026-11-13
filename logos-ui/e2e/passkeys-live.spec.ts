import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';

const enabled = process.env['E2E_PASSKEYS_LIVE'] === '1';
const authenticatorOptions = {
  protocol: 'ctap2' as const,
  transport: 'internal' as const,
  hasResidentKey: true,
  hasUserVerification: true,
  isUserVerified: true,
  automaticPresenceSimulation: true,
};

async function addPasskey(page: Page, label: string): Promise<void> {
  await page.getByRole('button', { name: 'Add passkey', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add passkey', exact: true });
  await dialog.getByLabel('Passkey name').fill(label);
  await dialog.getByRole('button', { name: 'Add passkey', exact: true }).click();
  await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible();
}

async function removePasskey(page: Page, label: string): Promise<void> {
  await page.getByRole('button', { name: `Remove ${label}`, exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove passkey?', exact: true });
  await expect(dialog).toContainText('any other application using this account');
  await dialog.getByRole('button', { name: 'Remove passkey', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Add passkey', exact: true })).toBeEnabled();
  await expect(page.getByRole('heading', { name: label, exact: true })).toHaveCount(0);
}

test('live localhost passkeys register, persist, revoke one and retain sign-in with another', async ({ page, context, browser, baseURL }) => {
  test.skip(!enabled, 'Requires an isolated localhost realm with the redirectless passkey provider and account API configured.');
  test.setTimeout(120_000);
  // Fail before credential mutation if this opt-in test targets a remote deployment.
  expect(new URL(baseURL!).hostname).toBe('localhost');
  const info = await (await page.request.get(`${baseURL}/api/info`)).json();
  expect(new URL(info.keycloak.issuer).hostname).toBe('localhost');
  expect(info.keycloak.passkey_rp_id || 'localhost').toBe('localhost');

  const prefix = `Browser test ${randomUUID()}`;
  const labels = [`${prefix} laptop`, `${prefix} backup`];
  const cdp = await context.newCDPSession(page);
  await cdp.send('WebAuthn.enable');
  let { authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', { options: authenticatorOptions });
  try {
    await page.goto('/settings');
    await expect(page.getByRole('heading', { name: 'Account settings', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Add passkey', exact: true })).toBeEnabled();
    await addPasskey(page, labels[0]);
    const first = (await cdp.send('WebAuthn.getCredentials', { authenticatorId })).credentials[0];
    expect(first).toBeTruthy();
    await cdp.send('WebAuthn.removeVirtualAuthenticator', { authenticatorId });
    ({ authenticatorId } = await cdp.send('WebAuthn.addVirtualAuthenticator', {
      options: { ...authenticatorOptions, transport: 'usb' },
    }));
    await addPasskey(page, labels[1]);
    const second = (await cdp.send('WebAuthn.getCredentials', { authenticatorId })).credentials[0];
    expect(second).toBeTruthy();
    await page.reload();
    for (const label of labels) await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible();

    await page.getByRole('button', { name: `Remove ${labels[0]}`, exact: true }).click();
    await page.getByRole('dialog', { name: 'Remove passkey?', exact: true }).getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('heading', { name: labels[0], exact: true })).toBeVisible();
    await removePasskey(page, labels[0]);
    await page.reload();
    await expect(page.getByRole('button', { name: 'Add passkey', exact: true })).toBeEnabled();
    await expect(page.getByRole('heading', { name: labels[0], exact: true })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: labels[1], exact: true })).toBeVisible();

    for (const [credential, works] of [[first, false], [second, true]] as const) {
      const loginContext = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] } });
      try {
        const loginPage = await loginContext.newPage();
        const loginCdp = await loginContext.newCDPSession(loginPage);
        await loginCdp.send('WebAuthn.enable');
        const authenticator = await loginCdp.send('WebAuthn.addVirtualAuthenticator', { options: authenticatorOptions });
        await loginCdp.send('WebAuthn.addCredential', { authenticatorId: authenticator.authenticatorId, credential });
        await loginPage.goto('/');
        await loginPage.getByRole('button', { name: 'Sign in with a passkey', exact: true }).click();
        if (works) {
          await expect(loginPage.locator('app-shell')).toBeVisible();
        } else {
          await expect(loginPage.getByRole('alert')).toContainText('Passkey authentication rejected');
          await expect(loginPage.locator('app-shell')).toHaveCount(0);
        }
      } finally {
        await loginContext.close();
      }
    }
  } finally {
    // Only this run's randomly named credentials are eligible for cleanup.
    await page.goto('/settings');
    await expect(page.getByRole('button', { name: 'Add passkey', exact: true })).toBeEnabled();
    for (const label of labels) {
      if (await page.getByRole('button', { name: `Remove ${label}`, exact: true }).count()) {
        await removePasskey(page, label);
      }
    }
    await cdp.send('WebAuthn.disable');
  }
});
