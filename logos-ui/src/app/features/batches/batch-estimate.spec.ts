import { estimateBatchCost, summarizeBatchInput } from './batch-estimate';
import { BatchModelPrice } from '../../core/services/batch.service';

const line = (body: object, url = '/v1/chat/completions') =>
  JSON.stringify({ custom_id: 'request-1', method: 'POST', url, body });
const prices: BatchModelPrice[] = [
  { model_name: 'alpha', input_price_per_k_tokens: 250_000, output_price_per_k_tokens: 1_000_000 },
  { model_name: 'beta', input_price_per_k_tokens: 100_000, output_price_per_k_tokens: 200_000 },
];

describe('Batch cost estimate', () => {
  it('counts nonempty request lines and estimates tokens from bodies only', () => {
    const body = { model: 'alpha', messages: [{ role: 'user', content: 'Hello' }] };
    const summary = summarizeBatchInput(`\n${line(body)}\r\n \r\n${line(body)}\n`);
    expect(summary.requestCount).toBe(2);
    expect(summary.inputTokens).toBe(2 * Math.ceil(JSON.stringify(body).length / 4));
    expect(summary.defaultResponseTokens).toBe(500);
  });

  it('averages output limits with a 500 token fallback for each missing limit', () => {
    const summary = summarizeBatchInput([
      line({ model: 'alpha', max_tokens: 100 }),
      line({ model: 'alpha', max_completion_tokens: 200, max_tokens: 900 }),
      line({ model: 'alpha', max_output_tokens: 400 }, '/v1/responses'),
      line({ model: 'alpha' }),
    ].join('\n'));
    expect(summary.defaultResponseTokens).toBe(300);
  });

  it('uses a fallback for invalid output limits', () => {
    for (const max_tokens of [0, -10, '100', 2.5, null]) {
      expect(summarizeBatchInput(line({ model: 'alpha', max_tokens })).defaultResponseTokens).toBe(500);
    }
  });

  it('prices each model separately and converts micro-cents per 1000 tokens to USD', () => {
    const summary = {
      requestCount: 3, inputTokens: 3000, defaultResponseTokens: 500,
      models: [
        { name: 'alpha', requestCount: 2, inputTokens: 2000, responseCount: 2 },
        { name: 'beta', requestCount: 1, inputTokens: 1000, responseCount: 1 },
      ],
    };
    expect(estimateBatchCost(summary, prices, 500)).toBeCloseTo(0.017);
    expect(estimateBatchCost(summary, prices, 1000)).toBeCloseTo(0.028);
    expect(estimateBatchCost(summary, prices, 0)).toBeCloseTo(0.006);
  });

  it('does not charge output tokens for embeddings or require an output price', () => {
    const summary = summarizeBatchInput(line({ model: 'alpha', input: 'Hello' }, '/v1/embeddings'));
    expect(summary.defaultResponseTokens).toBe(0);
    expect(estimateBatchCost(summary, [{ ...prices[0], output_price_per_k_tokens: null }], 500))
      .toBeCloseTo(summary.inputTokens * 250_000 / 100_000_000_000);
  });

  it('does not present partial estimates when a model or required price is missing', () => {
    const summary = summarizeBatchInput(`${line({ model: 'alpha' })}\n${line({ model: 'beta' })}`);
    expect(estimateBatchCost(summary, [prices[0]], 500)).toBeNull();
    expect(estimateBatchCost(summary, [prices[0], { ...prices[1], input_price_per_k_tokens: null }], 500)).toBeNull();
    expect(estimateBatchCost(summary, [prices[0], { ...prices[1], output_price_per_k_tokens: null }], 500)).toBeNull();
  });

  it('allows explicitly free models and rejects invalid response lengths', () => {
    const summary = summarizeBatchInput(line({ model: 'alpha' }));
    expect(estimateBatchCost(summary, [{ ...prices[0], input_price_per_k_tokens: 0, output_price_per_k_tokens: 0 }], 500)).toBe(0);
    for (const value of [null, -1, 1.5, NaN, Infinity]) {
      expect(estimateBatchCost(summary, prices, value)).toBeNull();
    }
  });

  it('rejects empty or malformed files instead of estimating only valid lines', () => {
    expect(() => summarizeBatchInput(' \n')).toThrow('no request lines');
    expect(() => summarizeBatchInput(`${line({ model: 'alpha' })}\n{`)).toThrow('line 2');
    for (const text of ['null', '{}', '{"body":[]}', line({ model: '' })]) {
      expect(() => summarizeBatchInput(text)).toThrow('line 1');
    }
  });
});
