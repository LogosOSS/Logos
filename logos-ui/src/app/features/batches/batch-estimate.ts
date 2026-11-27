import { BatchHistoricalModelCost } from '../../core/services/batch.service';

export interface BatchInputSummary {
  requestCount: number;
  models: { name: string; requestCount: number }[];
}

/** Count submitted request lines per model locally; no prompt text is transmitted to the estimate endpoint. */
export function summarizeBatchInput(text: string): BatchInputSummary {
  const models = new Map<string, number>();
  let requestCount = 0;
  for (const [index, line] of text.split(/\r?\n/).entries()) {
    if (!line.trim()) continue;
    let request;
    try {
      request = JSON.parse(line);
    } catch {
      throw new Error(`Cannot estimate cost: line ${index + 1} is not valid JSON.`);
    }
    const body = request?.body;
    if (!body || typeof body !== 'object' || Array.isArray(body)
        || typeof body.model !== 'string' || !body.model.trim()) {
      throw new Error(`Cannot estimate cost: line ${index + 1} needs a body with a model name.`);
    }
    models.set(body.model, (models.get(body.model) ?? 0) + 1);
    requestCount++;
  }
  if (!requestCount) throw new Error('Cannot estimate cost: the input file has no request lines.');
  return { requestCount, models: [...models].map(([name, count]) => ({ name, requestCount: count })) };
}

export function estimateHistoricalBatchCost(
  summary: BatchInputSummary,
  history: readonly BatchHistoricalModelCost[],
  mode: 'provider' | 'logos',
): number | null {
  const byName = new Map(history.map(item => [item.model_name, item]));
  let cost = 0;
  for (const model of summary.models) {
    const item = byName.get(model.name);
    const average = mode === 'provider' ? item?.provider_average_usd : item?.logos_average_usd;
    if (average == null || !Number.isFinite(average) || average < 0) return null;
    cost += model.requestCount * average;
  }
  return Number.isFinite(cost) ? cost : null;
}
