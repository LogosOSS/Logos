import { estimateHistoricalBatchCost, summarizeBatchInput } from './batch-estimate';

const line = (model: string) => JSON.stringify({ body: { model } });
const history = [
  { model_name: 'alpha', provider_average_usd: 0.01, logos_average_usd: 0.025 },
  { model_name: 'beta', provider_average_usd: 0.02, logos_average_usd: 0.09 },
];

describe('historical batch cost estimate', () => {
  it('counts each model and multiplies it by its own average for each execution mode', () => {
    const summary = summarizeBatchInput(`\n${line('alpha')}\r\n \r\n${line('alpha')}\n${line('beta')}`);
    expect(summary).toEqual({ requestCount: 3, models: [
      { name: 'alpha', requestCount: 2 }, { name: 'beta', requestCount: 1 },
    ] });
    expect(estimateHistoricalBatchCost(summary, history, 'provider')).toBeCloseTo(0.04);
    expect(estimateHistoricalBatchCost(summary, history, 'logos')).toBeCloseTo(0.14);
  });

  it('does not mix execution modes or report partial models as a total', () => {
    const summary = summarizeBatchInput(`${line('alpha')}\n${line('beta')}`);
    expect(estimateHistoricalBatchCost(summary, [history[0]], 'provider')).toBeNull();
    const oneMode = [history[0], { ...history[1], provider_average_usd: null }];
    expect(estimateHistoricalBatchCost(summary, oneMode, 'provider')).toBeNull();
    expect(estimateHistoricalBatchCost(summary, oneMode, 'logos')).toBeCloseTo(0.115);
  });

  it('keeps valid zero costs distinct from unavailable or invalid values', () => {
    const summary = summarizeBatchInput(line('alpha'));
    for (const value of [null, -1, NaN, Infinity]) {
      expect(estimateHistoricalBatchCost(summary, [{ ...history[0], provider_average_usd: value }], 'provider')).toBeNull();
    }
    expect(estimateHistoricalBatchCost(summary, [{ ...history[0], provider_average_usd: 0 }], 'provider')).toBe(0);
  });

  it('rejects empty or malformed files instead of estimating only valid lines', () => {
    expect(() => summarizeBatchInput(' \n')).toThrow('no request lines');
    expect(() => summarizeBatchInput(`${line('alpha')}\n{`)).toThrow('line 2');
    for (const text of ['null', '{}', '{"body":[]}', line('')]) {
      expect(() => summarizeBatchInput(text)).toThrow('line 1');
    }
  });
});
