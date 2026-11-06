import { notificationBody, notificationTarget } from './notification-target';
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

it('explains recovery in historical notifications without changing their destination', () => {
  const item = { type: 'benchmark_result_ready', body: 'Worker: 3 successful configurations. Status: interrupted_recovered.', payload: { model_id: 1, benchmark_id: 127 } } as AppNotification;
  expect(notificationBody(item)).toContain('Benchmark interrupted; worker configuration recovered.');
  expect(notificationTarget(item)?.queryParams.benchmark).toBe(127);
});
