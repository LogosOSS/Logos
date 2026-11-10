import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelRequests } from './model-requests';
import {
  ModelLicense,
  ModelRequest,
  ModelRequestService,
} from '../../core/services/model-request.service';
import { AuthService } from '../../core/auth/services/auth.service';

const makeRequest = (overrides: Partial<ModelRequest> = {}): ModelRequest => ({
  id: 1,
  name: 'gpt-5',
  request_count: 1,
  has_voted: false,
  is_gated: null,
  first_requesting_team: null,
  license_status: 'pending',
  license_approved: false,
  ...overrides,
});

const makeLicense = (overrides: Partial<ModelLicense> = {}): ModelLicense => ({
  status: 'available',
  license_id: 'apache-2.0',
  license_text: 'Apache License 2.0',
  approved: false,
  approved_at: null,
  ...overrides,
});

/** Let the fire-and-forget license load of the open dialog settle. */
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const makeUser = (teams: { id: number; name: string }[]) => ({
  user_id: 1,
  username: 'testuser',
  prename: 'Test',
  name: 'User',
  email: 'test@test.com',
  role: 'app_developer',
  teams,
});

describe('ModelRequests', () => {
  let page: ModelRequests;
  let list: ReturnType<typeof vi.fn>;
  let vote: ReturnType<typeof vi.fn>;
  let undoVote: ReturnType<typeof vi.fn>;
  let recheckGated: ReturnType<typeof vi.fn>;
  let getLicense: ReturnType<typeof vi.fn>;
  let approveLicense: ReturnType<typeof vi.fn>;
  let revokeLicense: ReturnType<typeof vi.fn>;

  const serviceMock = () => ({
    list,
    vote,
    undoVote,
    recheckGated,
    getLicense,
    approveLicense,
    revokeLicense,
  });

  beforeEach(async () => {
    list = vi.fn().mockResolvedValue([]);
    vote = vi.fn().mockResolvedValue(makeRequest({ has_voted: true }));
    undoVote = vi.fn().mockResolvedValue(makeRequest({ request_count: 0, has_voted: false }));
    recheckGated = vi.fn().mockResolvedValue(undefined);
    getLicense = vi.fn().mockResolvedValue(makeLicense());
    approveLicense = vi.fn().mockResolvedValue(
      makeLicense({ approved: true, approved_at: '2026-11-10T12:00:00Z' }),
    );
    revokeLicense = vi.fn().mockResolvedValue(makeLicense({ approved: false, approved_at: null }));
    TestBed.configureTestingModule({
      providers: [
        { provide: ModelRequestService, useValue: serviceMock() },
        {
          provide: AuthService,
          useValue: {
            currentUser: signal(makeUser([{ id: 2001, name: 'test-team' }])),
            role: signal('app_developer'),
          },
        },
      ],
    });
    page = TestBed.runInInjectionContext(() => new ModelRequests());
    await page.ngOnInit();
  });

  it('preselects the caller’s first team', async () => {
    expect(page.teamId()).toBe(2001);
  });

  it('loads the running list of requests', async () => {
    list.mockResolvedValue([makeRequest({ name: 'gpt-5', request_count: 2 })]);
    await page.refresh();
    expect(page.requests().map((r) => r.name)).toEqual(['gpt-5']);
    expect(page.loading()).toBe(false);
  });

  it('reports a load failure instead of crashing', async () => {
    list.mockRejectedValueOnce(new Error('down'));
    await page.refresh();
    expect(page.error()).toBe('Could not load model requests.');
    expect(page.requests()).toEqual([]);
  });

  it('re-verifies the flagged models before reloading the list', async () => {
    const order: string[] = [];
    recheckGated.mockImplementationOnce(async () => {
      order.push('recheck');
    });
    list.mockImplementationOnce(async () => {
      order.push('list');
      return [];
    });
    await page.refresh();
    expect(order).toEqual(['recheck', 'list']);
  });

  it('still loads the list when the recheck fails', async () => {
    recheckGated.mockRejectedValueOnce(new Error('Hub down'));
    list.mockResolvedValueOnce([makeRequest({ name: 'gpt-5' })]);
    await page.refresh();
    expect(page.requests().map((r) => r.name)).toEqual(['gpt-5']);
    expect(page.error()).toBeNull();
  });

  it('votes for a typed model and team, trimming and clearing the field on success', async () => {
    page.name.set('  gpt-5  ');
    await page.submit();
    expect(vote).toHaveBeenCalledWith('gpt-5', 2001);
    expect(page.name()).toBe('');
    expect(page.notice()).toContain('gpt-5');
  });

  it('submits for the switched team', async () => {
    page.onTeamChange('2002');
    expect(page.teamId()).toBe(2002);
    page.name.set('gpt-5');
    await page.submit();
    expect(vote).toHaveBeenCalledWith('gpt-5', 2002);
  });

  it('ignores an empty submission', async () => {
    page.name.set('   ');
    await page.submit();
    expect(vote).not.toHaveBeenCalled();
    expect(undoVote).not.toHaveBeenCalled();
  });

  it('ignores a submission while no team is selected', async () => {
    page.onTeamChange('');
    page.name.set('gpt-5');
    await page.submit();
    expect(vote).not.toHaveBeenCalled();
  });

  it('casts a vote on an unvoted listed model', async () => {
    await page.toggle(makeRequest({ id: 2, name: 'llama', request_count: 0, has_voted: false }));
    expect(vote).toHaveBeenCalledWith('llama', 2001);
    expect(undoVote).not.toHaveBeenCalled();
  });

  it('undoes a vote on a listed model the caller already voted for', async () => {
    await page.toggle(makeRequest({ id: 2, name: 'llama', request_count: 1, has_voted: true }));
    expect(undoVote).toHaveBeenCalledWith('llama');
    expect(vote).not.toHaveBeenCalled();
  });

  it('surfaces the backend error in the failure modal and keeps the typed name', async () => {
    vote.mockRejectedValueOnce({ error: { error: 'This model is already available in Logos.' } });
    page.name.set('gpt-4');
    await page.submit();
    // Vote failures go to the modal, not the inline load-error line ...
    expect(page.voteError()).toBe('This model is already available in Logos.');
    expect(page.error()).toBeNull();
    // ... and the field is kept so the user can correct the name instead of retyping.
    expect(page.name()).toBe('gpt-4');
  });

  it('clears the failure modal when closed', async () => {
    vote.mockRejectedValueOnce({ error: { error: 'This model is already available in Logos.' } });
    page.name.set('gpt-4');
    await page.submit();
    expect(page.voteError()).not.toBeNull();
    page.closeVoteError();
    expect(page.voteError()).toBeNull();
  });

  it('does nothing for a caller without teams', async () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ModelRequestService, useValue: serviceMock() },
        {
          provide: AuthService,
          useValue: {
            currentUser: signal(makeUser([])),
            role: signal('logos_admin'),
          },
        },
      ],
    });
    page = TestBed.runInInjectionContext(() => new ModelRequests());
    await page.ngOnInit();
    expect(page.teamId()).toBeNull();
    page.name.set('gpt-5');
    await page.submit();
    expect(vote).not.toHaveBeenCalled();
  });

  it('loads the stored license when the license dialog opens', async () => {
    const row = makeRequest({ id: 3, name: 'llama', license_status: 'available' });
    page.openLicenseDialog(row);
    expect(page.license()).toBeNull();
    expect(page.licenseLoading()).toBe(true);
    await tick();
    expect(getLicense).toHaveBeenCalledWith(3);
    expect(page.license()).toEqual(makeLicense());
    expect(page.acceptChecked()).toBe(false);
    expect(page.licenseLoading()).toBe(false);
  });

  it('surfaces a license load failure in the dialog', async () => {
    getLicense.mockRejectedValueOnce({ error: { error: 'Unknown requested model.' } });
    page.openLicenseDialog(makeRequest({ id: 3 }));
    await tick();
    expect(page.licenseError()).toBe('Unknown requested model.');
    expect(page.license()).toBeNull();
  });

  it('records the acceptance only with the checkbox set, then closes', async () => {
    const row = makeRequest({ id: 3, name: 'llama', license_status: 'available' });
    page.openLicenseDialog(row);
    await tick();
    await page.acceptLicense();
    expect(approveLicense).not.toHaveBeenCalled();
    expect(page.licenseTarget()).toBe(row);

    page.acceptChecked.set(true);
    await page.acceptLicense();
    expect(approveLicense).toHaveBeenCalledWith(3);
    expect(page.licenseTarget()).toBeNull();
    expect(page.notice()).toContain('accepted');
  });

  it('keeps the dialog open on a failed acceptance', async () => {
    approveLicense.mockRejectedValueOnce({
      error: { error: 'The license agreement cannot be accepted: no license information is available for this model yet.' },
    });
    page.openLicenseDialog(makeRequest({ id: 3, name: 'llama' }));
    await tick();
    page.acceptChecked.set(true);
    await page.acceptLicense();
    expect(page.licenseTarget()).not.toBeNull();
    expect(page.licenseError()).toBe(
      'The license agreement cannot be accepted: no license information is available for this model yet.',
    );
  });

  it('revokes the acceptance from the dialog, then closes', async () => {
    const row = makeRequest({ id: 3, name: 'llama', license_approved: true });
    page.openLicenseDialog(row);
    await tick();
    await page.revokeLicense();
    expect(revokeLicense).toHaveBeenCalledWith(3);
    expect(page.licenseTarget()).toBeNull();
    expect(page.notice()).toContain('revoked');
  });

  it('reports the flag title per license state', () => {
    expect(page.licenseFlagTitle(makeRequest({ license_status: 'available' }))).toBe(
      'License agreement not accepted yet',
    );
    expect(page.licenseFlagTitle(makeRequest({ license_status: 'no_license' }))).toBe(
      'No license information found on Hugging Face',
    );
    expect(page.licenseFlagTitle(makeRequest({ license_status: 'pending', is_gated: true }))).toBe(
      'The model is gated — its license can only be fetched once Hugging Face grants access',
    );
    expect(page.licenseFlagTitle(makeRequest({ license_status: 'pending', is_gated: false }))).toBe(
      'The license has not been fetched yet',
    );
  });
});

describe('the gated model flag', () => {
  const GATED = makeRequest({ id: 7, name: 'meta-llama/llama-3.1-8b', is_gated: true });
  const OPEN = makeRequest({ id: 8, name: 'openai/gpt-5', is_gated: false });

  /**
   * A rendered page (the flag's role/gated gating lives in the template),
   * listing one gated and one open request.
   */
  async function renderPage(role: 'logos_admin' | 'app_developer') {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ModelRequests],
      providers: [
        {
          provide: ModelRequestService,
          useValue: {
            list: vi.fn().mockResolvedValue([GATED, OPEN]),
            vote: vi.fn(),
            undoVote: vi.fn(),
            recheckGated: vi.fn().mockResolvedValue(undefined),
            getLicense: vi.fn().mockResolvedValue(makeLicense({ status: 'pending', license_id: null, license_text: null })),
            approveLicense: vi.fn(),
            revokeLicense: vi.fn(),
          },
        },
        {
          provide: AuthService,
          useValue: { currentUser: signal(makeUser([])), role: signal(role) },
        },
      ],
    }).compileComponents();
    const fx = TestBed.createComponent(ModelRequests);
    fx.detectChanges();
    // Let the async list load settle, then render the loaded rows.
    await new Promise((resolve) => setTimeout(resolve, 0));
    fx.detectChanges();
    return fx;
  }

  it('flags a gated model for the Logos admin and nothing else', async () => {
    const fx = await renderPage('logos_admin');
    const flags = fx.nativeElement.querySelectorAll('.gated-flag');
    expect(flags).toHaveLength(1);
    expect(flags[0].title).toBe('Model repository is gated');
    expect(flags[0].closest('td')?.textContent).toContain('meta-llama/llama-3.1-8b');
    fx.destroy();
  });

  it('stays out for roles that cannot accept the license', async () => {
    const fx = await renderPage('app_developer');
    expect(fx.nativeElement.querySelectorAll('.gated-flag')).toHaveLength(0);
    fx.destroy();
  });

  it('opens the license dialog on click and the Hugging Face page from it', async () => {
    const fx = await renderPage('logos_admin');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      (fx.nativeElement.querySelector('.gated-flag') as HTMLButtonElement).click();
      fx.detectChanges();

      // The dialog renders into the overlay container, outside the component.
      expect(document.body.textContent).toContain(
        "You have to accept this gated model's conditions on Hugging Face to access its files and content.",
      );
      // Acceptance is an access request, not an instant grant — say so.
      expect(document.body.textContent).toContain(
        "The repository owner must still approve the access request.",
      );
      const confirm = [...document.body.querySelectorAll('button')].find(
        (b) => b.textContent?.trim() === 'Go to HuggingFace',
      );
      expect(confirm).toBeDefined();

      confirm!.click();
      fx.detectChanges();
      expect(openSpy).toHaveBeenCalledWith(
        'https://huggingface.co/meta-llama/llama-3.1-8b',
        '_blank',
        'noopener,noreferrer',
      );
      expect(fx.componentInstance.gatedTarget()).toBeNull();
    } finally {
      openSpy.mockRestore();
      fx.destroy();
      // A dialog closed mid-test may still be running its exit animation;
      // drop the overlay so it cannot leak into the next test's button search.
      document.querySelector('cdk-overlay-container')?.remove();
    }
  });

  it('dismisses the dialog without opening a tab on cancel', async () => {
    const fx = await renderPage('logos_admin');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      (fx.nativeElement.querySelector('.gated-flag') as HTMLButtonElement).click();
      fx.detectChanges();
      const cancel = [...document.body.querySelectorAll('button')].find(
        (b) => b.textContent?.trim() === 'Cancel',
      );
      expect(cancel).toBeDefined();

      cancel!.click();
      fx.detectChanges();
      expect(openSpy).not.toHaveBeenCalled();
      expect(fx.componentInstance.gatedTarget()).toBeNull();
    } finally {
      openSpy.mockRestore();
      fx.destroy();
      document.querySelector('cdk-overlay-container')?.remove();
    }
  });
});

describe('the license agreement column', () => {
  const OPEN_OPEN = makeRequest({
    id: 11,
    name: 'openai/gpt-5',
    is_gated: false,
    license_status: 'available',
  });
  const OPEN_ACCEPTED = makeRequest({
    id: 12,
    name: 'mistral/mistral-7b',
    is_gated: false,
    license_status: 'available',
    license_approved: true,
  });
  const GATED = makeRequest({
    id: 13,
    name: 'meta-llama/llama-3.1-8b',
    is_gated: true,
    license_status: 'pending',
  });

  const LICENSES: Record<number, ModelLicense> = {
    [OPEN_OPEN.id]: makeLicense({
      status: 'available',
      license_id: 'apache-2.0',
      license_text: 'Apache License 2.0 — full text',
    }),
    [OPEN_ACCEPTED.id]: makeLicense({
      status: 'available',
      license_id: 'mit',
      license_text: 'MIT License',
      approved: true,
      approved_at: '2026-11-10T12:00:00Z',
    }),
    [GATED.id]: makeLicense({
      status: 'pending',
      license_id: null,
      license_text: null,
    }),
  };

  const serviceMock = () => ({
    list: vi.fn().mockResolvedValue([OPEN_OPEN, OPEN_ACCEPTED, GATED]),
    vote: vi.fn(),
    undoVote: vi.fn(),
    recheckGated: vi.fn().mockResolvedValue(undefined),
    getLicense: vi.fn(async (id: number) => LICENSES[id]),
    approveLicense: vi.fn(async (id: number) => ({
      ...LICENSES[id],
      approved: true,
      approved_at: '2026-11-10T12:00:00Z',
    })),
    revokeLicense: vi.fn(async (id: number) => ({ ...LICENSES[id], approved: false, approved_at: null })),
  });

  async function renderPage(role: 'logos_admin' | 'app_developer') {
    const service = serviceMock();
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [ModelRequests],
      providers: [
        { provide: ModelRequestService, useValue: service },
        {
          provide: AuthService,
          useValue: { currentUser: signal(makeUser([])), role: signal(role) },
        },
      ],
    }).compileComponents();
    const fx = TestBed.createComponent(ModelRequests);
    fx.detectChanges();
    await new Promise((resolve) => setTimeout(resolve, 0));
    fx.detectChanges();
    return { fx, service };
  }

  const rowOf = (fx: { nativeElement: HTMLElement }, name: string) =>
    [...fx.nativeElement.querySelectorAll('tr')].find((tr) => tr.textContent?.includes(name))!;

  const buttonOn = (root: ParentNode, label: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);

  afterEach(() => {
    // A dialog closed mid-test may still be running its exit animation;
    // drop the overlay so it cannot leak into the next test's button search.
    document.querySelector('cdk-overlay-container')?.remove();
  });

  it('shows the column for the Logos admin and nothing for other roles', async () => {
    const { fx } = await renderPage('logos_admin');
    expect(fx.nativeElement.textContent).toContain('License agreement status');
    expect(fx.nativeElement.querySelectorAll('.license-state')).toHaveLength(3);
    fx.destroy();

    const dev = await renderPage('app_developer');
    expect(dev.fx.nativeElement.textContent).not.toContain('License agreement status');
    expect(dev.fx.nativeElement.querySelectorAll('.license-state')).toHaveLength(0);
    dev.fx.destroy();
  });

  it('shows a green check for an accepted license and a red cross otherwise', async () => {
    const { fx } = await renderPage('logos_admin');
    expect(rowOf(fx, 'openai/gpt-5').querySelector('.license-state--open')).toBeTruthy();
    expect(rowOf(fx, 'mistral/mistral-7b').querySelector('.license-state--ok')).toBeTruthy();
    expect(rowOf(fx, 'meta-llama/llama-3.1-8b').querySelector('.license-state--open')).toBeTruthy();
    fx.destroy();
  });

  it('opens the license dialog with the stored text, the checkbox and a disabled Save', async () => {
    const { fx } = await renderPage('logos_admin');
    (rowOf(fx, 'openai/gpt-5').querySelector('.license-state') as HTMLButtonElement).click();
    fx.detectChanges();
    await tick();
    fx.detectChanges();

    expect(document.body.textContent).toContain('Apache License 2.0 — full text');
    const checkbox = document.querySelector('.license-accept input') as HTMLInputElement;
    expect(checkbox).toBeTruthy();
    expect(checkbox.checked).toBe(false);

    const save = buttonOn(document.body, 'Save');
    expect(save).toBeDefined();
    expect(save!.disabled).toBe(true);

    checkbox.click();
    fx.detectChanges();
    expect(save!.disabled).toBe(false);
    fx.destroy();
  });

  it('saves the acceptance from the dialog, closes it and refreshes the list', async () => {
    const { fx, service } = await renderPage('logos_admin');
    (rowOf(fx, 'openai/gpt-5').querySelector('.license-state') as HTMLButtonElement).click();
    fx.detectChanges();
    await tick();
    fx.detectChanges();

    (document.querySelector('.license-accept input') as HTMLInputElement).click();
    fx.detectChanges();
    buttonOn(document.body, 'Save')!.click();
    await tick();
    fx.detectChanges();

    expect(service.approveLicense).toHaveBeenCalledWith(11);
    expect(fx.componentInstance.licenseTarget()).toBeNull();
    expect(fx.componentInstance.notice()).toContain('accepted');
    // The refresh after the acceptance re-reads the list.
    expect(service.list).toHaveBeenCalledTimes(2);
    fx.destroy();
  });

  it('shows the accepted state with a revoke action and no checkbox', async () => {
    const { fx, service } = await renderPage('logos_admin');
    (rowOf(fx, 'mistral/mistral-7b').querySelector('.license-state') as HTMLButtonElement).click();
    fx.detectChanges();
    await tick();
    fx.detectChanges();

    expect(document.body.textContent).toContain('MIT License');
    expect(document.body.textContent).toContain('Accepted on');
    expect(document.querySelector('.license-accept')).toBeNull();

    buttonOn(document.body, 'Revoke acceptance')!.click();
    await tick();
    fx.detectChanges();
    expect(service.revokeLicense).toHaveBeenCalledWith(12);
    expect(fx.componentInstance.licenseTarget()).toBeNull();
    expect(fx.componentInstance.notice()).toContain('revoked');
    fx.destroy();
  });

  it('offers the Hugging Face page for a pending gated row', async () => {
    const { fx } = await renderPage('logos_admin');
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    try {
      (rowOf(fx, 'meta-llama/llama-3.1-8b').querySelector('.license-state') as HTMLButtonElement).click();
      fx.detectChanges();
      await tick();
      fx.detectChanges();

      expect(document.body.textContent).toContain(
        'its license is only readable on Hugging Face after the',
      );

      buttonOn(document.body, 'Go to HuggingFace')!.click();
      fx.detectChanges();
      expect(openSpy).toHaveBeenCalledWith(
        'https://huggingface.co/meta-llama/llama-3.1-8b',
        '_blank',
        'noopener,noreferrer',
      );
      expect(fx.componentInstance.licenseTarget()).toBeNull();
    } finally {
      openSpy.mockRestore();
      fx.destroy();
    }
  });
});
