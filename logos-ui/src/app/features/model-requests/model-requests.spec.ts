import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ModelRequests } from './model-requests';
import { ModelRequest, ModelRequestService } from '../../core/services/model-request.service';
import { AuthService } from '../../core/auth/services/auth.service';

const makeRequest = (overrides: Partial<ModelRequest> = {}): ModelRequest => ({
  id: 1,
  name: 'gpt-5',
  request_count: 1,
  has_voted: false,
  is_gated: null,
  first_requesting_team: null,
  ...overrides,
});

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

  const serviceMock = () => ({
    list,
    vote,
    undoVote,
    recheckGated,
  });

  beforeEach(async () => {
    list = vi.fn().mockResolvedValue([]);
    vote = vi.fn().mockResolvedValue(makeRequest({ has_voted: true }));
    undoVote = vi.fn().mockResolvedValue(makeRequest({ request_count: 0, has_voted: false }));
    recheckGated = vi.fn().mockResolvedValue(undefined);
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
    list.mockRejectedValue(new Error('down'));
    await page.refresh();
    expect(page.error()).toBe('Could not load model requests.');
    expect(page.requests()).toEqual([]);
  });

  it('renders the stored list first and reloads it after the recheck', async () => {
    // The recheck is a slow remote scan, so it must not hold the (local)
    // list back: the stored rows render first, the verdicts reload after.
    const order: string[] = [];
    list.mockImplementation(async () => {
      order.push('list');
      return [];
    });
    recheckGated.mockImplementationOnce(async () => {
      order.push('recheck');
    });
    await page.refresh();
    expect(order).toEqual(['list', 'recheck', 'list']);
  });

  it('still loads the list when the recheck fails', async () => {
    recheckGated.mockRejectedValueOnce(new Error('Hub down'));
    list.mockResolvedValue([makeRequest({ name: 'gpt-5' })]);
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

