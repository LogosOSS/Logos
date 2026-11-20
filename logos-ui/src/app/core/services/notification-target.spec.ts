import { notificationBody, notificationModelUrl, notificationTarget } from './notification-target';
import { AppNotification } from './notification.service';

describe('Benchmark notification destinations', () => {
  const notification = (payload: unknown) => ({ type: 'benchmark_result_ready', payload } as AppNotification);
  it('supports captured results and historical benchmark notifications', () => {
    expect(notificationTarget(notification({ model_id: 1, benchmark_id: 124, best_benchmark_id: 115 }))?.queryParams.benchmark).toBe(124);
    expect(notificationTarget(notification({ model_id: 1, best_benchmark_id: 115 }))?.queryParams.benchmark).toBe(115);
  });
  it.each([null, {}, { model_id: 1 }, { model_id: 1, benchmark_id: null, best_benchmark_id: 115 }, { model_id: -1, benchmark_id: 3 },
    { model_id: 1, benchmark_id: 'https://example.com' }, { model_id: 1, benchmark_id: 1.5 }])(
    'does not navigate using missing or invalid identifiers: %j', payload => {
      expect(notificationTarget(notification(payload))).toBeNull();
    });
});

describe('Gated model notification destinations', () => {
  const notification = (payload: unknown) => ({ type: 'gated_model_requested', payload } as AppNotification);
  it('points at the model page on Hugging Face', () => {
    expect(notificationModelUrl(notification({ model: 'meta-llama/llama-3.1-8b' }))).toBe(
      'https://huggingface.co/meta-llama/llama-3.1-8b',
    );
  });
  it.each([
    null, {}, { model: null }, { model: 42 }, { model: '' },
    { model: 'no-slash' }, { model: 'a/b/c' }, { model: 'x y/z' },
    { model: 'https://huggingface.co/x/y' }, { model: 'x@y/z' }, { model: 'x/y?redirect=1' },
    { model: 'x/y\nHost: evil.example' },
  ])('does not open a URL for a malformed model name: %j', payload => {
    expect(notificationModelUrl(notification(payload))).toBeNull();
  });
  it('ignores other notification types', () => {
    expect(notificationModelUrl({ type: 'batch_job_finished', payload: { model: 'x/y' } } as AppNotification)).toBeNull();
  });
});

it('explains recovery in historical notifications without changing their destination', () => {
  const item = { type: 'benchmark_result_ready', body: 'Worker: 3 successful configurations. Status: interrupted_recovered.', payload: { model_id: 1, benchmark_id: 127 } } as AppNotification;
  expect(notificationBody(item)).toContain('Benchmark interrupted; worker configuration recovered.');
  expect(notificationTarget(item)?.queryParams.benchmark).toBe(127);
});
