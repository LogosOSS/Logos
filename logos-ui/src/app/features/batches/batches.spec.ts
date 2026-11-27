import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';

import { Batches } from './batches';
import { BATCH_PREPARATION_PROMPT } from './batch-preparation-prompt';
import { BatchObject } from '../../core/services/batch.service';
import { summarizeBatchInput } from './batch-estimate';

/**
 * The page's own judgements: what counts as finished, and how much of a batch
 * has been done. Both drive the poll loop and the buttons, so they are worth
 * pinning down independently of the template.
 */

const batch = (overrides: Partial<BatchObject> = {}): BatchObject => ({
  id: 'batch_1',
  object: 'batch',
  status: 'in_progress',
  endpoint: '/v1/chat/completions',
  input_file_id: 'file-in',
  output_file_id: null,
  created_at: 0,
  completed_at: null,
  ...overrides,
});

describe('Batches', () => {
  let page: Batches;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    page = TestBed.runInInjectionContext(() => new Batches());
  });

  it('treats every state the job will not leave on its own as terminal', () => {
    for (const status of ['completed', 'failed', 'expired', 'cancelled']) {
      expect(page.isTerminal(batch({ status }))).toBe(true);
    }
    for (const status of ['validating', 'in_progress', 'cancelling', 'finalizing']) {
      expect(page.isTerminal(batch({ status }))).toBe(false);
    }
  });

  it('keeps polling only while something is still moving', () => {
    page.batches.set([batch({ status: 'completed' }), batch({ id: 'b2', status: 'in_progress' })]);
    expect(page.hasRunning()).toBe(true);

    page.batches.set([batch({ status: 'completed' })]);
    expect(page.hasRunning()).toBe(false);
  });

  it('counts failed requests as done, and says how many failed', () => {
    expect(page.progressOf(batch({ request_counts: { total: 10, completed: 6, failed: 2 } })))
      .toBe('8 / 10 (2 failed)');
    expect(page.progressOf(batch({ request_counts: { total: 10, completed: 10, failed: 0 } })))
      .toBe('10 / 10');
    // A forwarded batch reports no counts until the provider does.
    expect(page.progressOf(batch())).toBe('—');
  });

  it('shows two conditional estimates for automatic execution, never a blended number', () => {
    page.inputSummary.set(summarizeBatchInput('{"body":{"model":"model"}}'));
    page.history.set([{ model_name: 'model', provider_average_usd: 0.05, logos_average_usd: 0.12 }]);
    expect(page.estimates().map(row => row.usd)).toEqual([0.05, 0.12]);
    page.execution.set('provider');
    expect(page.estimates().map(row => row.usd)).toEqual([0.05]);
    page.execution.set('logos');
    expect(page.estimates().map(row => row.usd)).toEqual([0.12]);
  });

  it('clears old history and ignores a response for a previously selected key', async () => {
    const http = TestBed.inject(HttpTestingController);
    page.selectedKeyId.set(1);
    const first = page.loadHistory();
    const firstRequest = http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=1');
    page.selectedKeyId.set(2);
    const second = page.loadHistory();
    const secondRequest = http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=2');
    expect(page.history()).toEqual([]);
    firstRequest.flush([{ model_name: 'old', provider_average_usd: 1, logos_average_usd: 1 }]);
    await first;
    expect(page.history()).toEqual([]);
    expect(page.loadingHistory()).toBe(true);
    secondRequest.flush([{ model_name: 'current', provider_average_usd: 2, logos_average_usd: 2 }]);
    await second;
    expect(page.history()[0].model_name).toBe('current');
    expect(page.loadingHistory()).toBe(false);
    http.verify();
  });

  it('ignores an old estimate response after selecting the original key again', async () => {
    const http = TestBed.inject(HttpTestingController);
    page.selectedKeyId.set(1);
    const old = page.loadHistory();
    const oldRequest = http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=1');
    page.selectedKeyId.set(2);
    const middle = page.loadHistory();
    const middleRequest = http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=2');
    page.selectedKeyId.set(1);
    const current = page.loadHistory();
    const currentRequest = http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=1');
    currentRequest.flush([{ model_name: 'new', provider_average_usd: 9, logos_average_usd: 8 }]);
    await current;
    oldRequest.flush([{ model_name: 'old', provider_average_usd: 1, logos_average_usd: 2 }]);
    middleRequest.flush([]);
    await Promise.all([old, middle]);
    expect(page.history()[0].model_name).toBe('new');
    http.verify();
  });

  it('reports unavailable history without reusing an earlier estimate', async () => {
    const http = TestBed.inject(HttpTestingController);
    page.selectedKeyId.set(1);
    page.history.set([{ model_name: 'model', provider_average_usd: 1, logos_average_usd: 1 }]);
    const loading = page.loadHistory();
    http.expectOne('/api/logosdb/batches/historical-costs?apiKeyId=1')
      .flush('Unavailable', { status: 503, statusText: 'Unavailable' });
    await loading;
    expect(page.history()).toEqual([]);
    expect(page.historyError()).toContain('Estimate unavailable');
    expect(page.loadingHistory()).toBe(false);
    http.verify();
  });

  it('ignores an older file read and clears estimates for an invalid replacement', async () => {
    let finishOld!: (text: string) => void;
    const oldFile = { text: () => new Promise<string>(resolve => { finishOld = resolve; }) } as File;
    const newFile = { text: async () => '{"body":{"model":"new","max_tokens":100}}' } as File;
    const select = (file: File) => page.onFileSelected({ target: { files: [file] } } as unknown as Event);
    const oldRead = select(oldFile);
    await select(newFile);
    finishOld('{"body":{"model":"old","max_tokens":900}}');
    await oldRead;
    expect(page.inputSummary()?.models[0].name).toBe('new');
    expect(page.readingFile()).toBe(false);
    await select({ text: async () => '{' } as File);
    expect(page.inputSummary()).toBeNull();
    expect(page.estimates().every(row => row.usd === null)).toBe(true);
    expect(page.estimateError()).toContain('line 1');
  });

  describe('the preparation prompt button', () => {
    let originalClipboard: PropertyDescriptor | undefined;

    beforeEach(() => {
      originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    });

    afterEach(() => {
      if (originalClipboard) {
        Object.defineProperty(navigator, 'clipboard', originalClipboard);
      } else {
        Reflect.deleteProperty(navigator, 'clipboard');
      }
    });

    async function renderWithClipboard(writeText: (value: string) => Promise<void>) {
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      const fixture = TestBed.createComponent(Batches);
      const http = TestBed.inject(HttpTestingController);
      fixture.detectChanges();
      http.expectOne('/api/logosdb/batches/keys').flush([
        { id: 1, name: 'Synthetic key name', team_id: null, environment: null },
      ]);
      await Promise.resolve();
      http.expectOne('/api/logosdb/batches?apiKeyId=1').flush({ data: [] });
      await fixture.whenStable();
      fixture.detectChanges();
      const host: HTMLElement = fixture.nativeElement;
      const button = [...host.querySelectorAll('button')]
        .find(candidate => candidate.textContent?.trim() === 'Copy prompt for an agent');
      expect(button).toBeDefined();
      button!.click();
      await fixture.whenStable();
      fixture.detectChanges();
      http.verify(); // Copying must not make network requests, including uploads or submission.
      return { fixture, host };
    }

    it('copies only the static preparation prompt and announces success', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      const { fixture, host } = await renderWithClipboard(writeText);
      expect(writeText).toHaveBeenCalledExactlyOnceWith(BATCH_PREPARATION_PROMPT);
      expect(writeText.mock.calls[0][0]).not.toContain('Synthetic key name');
      expect(host.querySelector('[role="status"]')?.textContent).toContain('Prompt copied.');
      expect(host.querySelector('textarea')).toBeNull();
      fixture.destroy();
    });

    it('makes the same prompt selectable when the clipboard is unavailable', async () => {
      const { fixture, host } = await renderWithClipboard(
        vi.fn().mockRejectedValue(new Error('clipboard denied')),
      );
      const textarea = host.querySelector('textarea');
      expect(host.querySelector('[role="status"]')?.textContent).toContain('Clipboard unavailable.');
      expect(textarea?.readOnly).toBe(true);
      expect(textarea?.value).toBe(BATCH_PREPARATION_PROMPT);
      fixture.destroy();
    });
  });
});
