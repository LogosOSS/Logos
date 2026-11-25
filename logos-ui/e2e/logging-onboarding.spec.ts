import { randomUUID } from 'node:crypto';
import { expect, test, type Page } from '@playwright/test';
import { DEVELOPER_USER } from './fixtures';

const title = 'Choose your logging level';

function teamCard(page: Page, name: string) {
  return page.locator('.team-card').filter({ has: page.getByText(name, { exact: true }) });
}

test('opening a personal developer key prompts once per team, including teams joined during the current session', async ({
  page: admin, browser, baseURL,
}) => {
  const adminRequest = admin.waitForRequest(request => new URL(request.url()).pathname === '/api/me');
  await admin.goto('/statistics');
  const authorization = await (await adminRequest).headerValue('authorization');
  expect(authorization, 'The administrator session must authorize team setup').toBeTruthy();
  const headers = { Authorization: authorization! };
  const teams: { id: number; name: string }[] = [];

  const context = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] } });
  try {
    const users = await admin.request.get('/api/users', { headers });
    await expect(users).toBeOK();
    const email = `${DEVELOPER_USER.username}@test.de`;
    let developer = (await users.json() as { id: number; email: string }[]).find(user => user.email === email);
    if (!developer) {
      const created = await admin.request.post('/api/users', {
        headers,
        data: { email, prename: 'Henriette', name: 'Huhn', role: DEVELOPER_USER.role },
      });
      await expect(created).toBeOK();
      developer = (await created.json()) as { id: number; email: string };
    }
    const userId = developer.id;

    const suffix = randomUUID().slice(0, 8);
    const createTeam = async (label: string) => {
      const team = await admin.request.post('/api/teams', {
        headers,
        data: { name: `Logging ${label} ${suffix}`, privacy_scope: 'LOCAL' },
      });
      await expect(team).toBeOK();
      const created = await team.json() as { id: number; name: string };
      teams.push(created);
      return created;
    };
    const teamA = await createTeam('A');
    const teamB = await createTeam('B');
    const teamC = await createTeam('C');
    const addMember = async (teamId: number) => {
      const added = await admin.request.post(`/api/teams/${teamId}/members`, {
        headers, data: { user_id: userId, is_owner: false },
      });
      await expect(added).toBeOK();
    };
    await addMember(teamA.id);
    await addMember(teamB.id);

    const page = await context.newPage();
    const dialog = page.getByRole('dialog', { name: title, exact: true });
    const workspace = page.getByRole('heading', { name: 'My Workspace', exact: true });
    await page.goto('/');
    await page.getByRole('button', { name: 'Sign in with TUM', exact: true }).click();
    await page.locator('#username').fill(DEVELOPER_USER.username);
    await page.locator('#password').fill(DEVELOPER_USER.password);
    await page.locator('#kc-login').click();
    await expect(workspace).toBeVisible();
    await expect(teamCard(page, teamA.name)).toBeVisible();
    await expect(teamCard(page, teamB.name)).toBeVisible();
    await expect(teamCard(page, teamC.name)).toHaveCount(0);
    await expect(dialog).toHaveCount(0);

    const openKey = (name: string) => teamCard(page, name).getByRole('button', { name: 'Expand models', exact: true }).click();
    await openKey(teamA.name);
    await expect(dialog).toContainText(teamA.name);
    await expect(dialog).not.toContainText(teamB.name);
    await dialog.getByRole('button', { name: 'Keep billing-only logging', exact: true }).click();
    await expect(dialog).toHaveCount(0);

    await openKey(teamB.name);
    await expect(dialog).toContainText(teamB.name);
    await expect(dialog).not.toContainText(teamA.name);
    await dialog.getByRole('button', { name: 'Enable full logging', exact: true }).click();
    await expect(dialog).toHaveCount(0);

    // Visit another page, then add C while the same application session is open.
    await page.getByRole('link', { name: 'Models', exact: true }).click();
    await expect(page).toHaveURL(/\/models$/);
    await addMember(teamC.id);
    await page.getByRole('link', { name: 'My Workspace', exact: true }).click();
    await expect(workspace).toBeVisible();
    await expect(teamCard(page, teamC.name)).toBeVisible();
    await expect(dialog).toHaveCount(0);

    await openKey(teamA.name);
    await expect(teamCard(page, teamA.name).getByRole('button', { name: 'Collapse models' })).toBeVisible();
    await expect(teamCard(page, teamA.name).getByRole('button', { name: 'BILLING', exact: true }))
      .toHaveClass(/btn-toggle--active/);
    await expect(dialog).toHaveCount(0);
    await openKey(teamB.name);
    await expect(teamCard(page, teamB.name).getByRole('button', { name: 'Collapse models' })).toBeVisible();
    await expect(teamCard(page, teamB.name).getByRole('button', { name: 'FULL', exact: true }))
      .toHaveClass(/btn-toggle--active/);
    await expect(dialog).toHaveCount(0);

    await openKey(teamC.name);
    await expect(dialog).toContainText(teamC.name);
    await dialog.getByRole('button', { name: 'Keep billing-only logging', exact: true }).click();
    await expect(dialog).toHaveCount(0);
  } finally {
    await context.close();
    await Promise.all(teams.map(async team => {
      const deleted = await admin.request.delete(`/api/teams/${team.id}`, { headers });
      await expect(deleted).toBeOK();
    }));
  }
});
