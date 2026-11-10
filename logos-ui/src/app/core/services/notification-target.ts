import { AppNotification } from './notification.service';

function identifier(value: unknown): number | null {
  if (typeof value !== 'number') return null;
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** Resolve captured identifiers to an application route, never to a payload-supplied URL. */
export function notificationTarget(item: AppNotification) {
  if (item.type !== 'benchmark_result_ready' || !item.payload || typeof item.payload !== 'object') return null;
  const payload = item.payload as Record<string, unknown>;
  const model = identifier(payload['model_id']);
  const benchmark = identifier('benchmark_id' in payload
    ? payload['benchmark_id'] : payload['best_benchmark_id'] ?? payload['reference_id']);
  if (model === null || benchmark === null) return null;
  return { commands: ['/models', model, 'details'], queryParams: { tab: 'performance', benchmark } };
}


/** Older inbox records retain their original body; translate the technical recovery code when rendering. */
export function notificationBody(item: AppNotification): string | null {
  if (item.type !== 'benchmark_result_ready') return item.body;
  return item.body?.replace('Status: interrupted_recovered.', 'Benchmark interrupted; worker configuration recovered.')
    .replace('Status: interrupted_recovery.', 'Benchmark interrupted; worker recovery in progress.') ?? null;
}
