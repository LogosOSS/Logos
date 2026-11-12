import { BatchModelPrice } from '../../core/services/batch.service';

export interface BatchInputSummary {
  requestCount: number;
  inputTokens: number;
  defaultResponseTokens: number;
  models: { name: string; requestCount: number; inputTokens: number; responseCount: number }[];
}

/** Approximate input tokens from each JSON body; envelope fields do not contribute. */
export function summarizeBatchInput(text: string): BatchInputSummary {
  const models = new Map<string, BatchInputSummary['models'][number]>();
  let requestCount = 0;
  let inputTokens = 0;
  let responseCount = 0;
  let responseTokens = 0;
  const lines = text.split(/\r?\n/);
  for (const [index, line] of lines.entries()) {
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
    const tokens = Math.ceil(JSON.stringify(body).length / 4);
    const model = models.get(body.model) ?? {
      name: body.model, requestCount: 0, inputTokens: 0, responseCount: 0,
    };
    model.requestCount++;
    model.inputTokens += tokens;
    if (request.url !== '/v1/embeddings') {
      const limit = [body.max_completion_tokens, body.max_output_tokens, body.max_tokens]
        .find(value => Number.isSafeInteger(value) && value > 0) ?? 500;
      responseTokens += limit;
      responseCount++;
      model.responseCount++;
    }
    models.set(body.model, model);
    requestCount++;
    inputTokens += tokens;
  }
  if (!requestCount) throw new Error('Cannot estimate cost: the input file has no request lines.');
  return {
    requestCount, inputTokens,
    defaultResponseTokens: responseCount ? Math.ceil(responseTokens / responseCount) : 0,
    models: [...models.values()],
  };
}

/** Convert micro-cents per 1000 tokens to USD; unknown rates never count as free. */
export function estimateBatchCost(
  summary: BatchInputSummary,
  prices: readonly BatchModelPrice[],
  averageResponseTokens: number | null,
): number | null {
  if (averageResponseTokens === null || !Number.isSafeInteger(averageResponseTokens) || averageResponseTokens < 0) {
    return null;
  }
  let cost = 0;
  for (const model of summary.models) {
    const price = prices.find(item => item.model_name === model.name);
    const input = price?.input_price_per_k_tokens;
    const output = price?.output_price_per_k_tokens;
    const outputTokens = model.responseCount * averageResponseTokens;
    if (input == null || !Number.isFinite(input) || input < 0
        || (outputTokens > 0 && (output == null || !Number.isFinite(output) || output < 0))) return null;
    cost += (model.inputTokens * input + outputTokens * (output ?? 0)) / 100_000_000_000;
  }
  return Number.isFinite(cost) ? cost : null;
}
