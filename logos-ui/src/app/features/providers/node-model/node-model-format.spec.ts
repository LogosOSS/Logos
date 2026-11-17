import { describe, expect, it } from 'vitest';

import {
  advancedSettings,
  describePlacement,
  entrySettings,
  formatGb,
  formatSeconds,
  formatTokens,
  kvToGb,
  mbToGb,
} from './node-model-format';

describe('node model formatting', () => {
  it('reads KV cache sizes with and without a unit', () => {
    expect(kvToGb('4G')).toBe(4);
    expect(kvToGb('4096M')).toBe(4);
    expect(kvToGb('2.5g')).toBe(2.5);
    expect(kvToGb('4194304K')).toBe(4);
    expect(kvToGb('1T')).toBe(1024);
    expect(kvToGb('4294967296')).toBe(4);
    expect(kvToGb(3)).toBe(3);
  });

  it('refuses what is not a size', () => {
    expect(kvToGb('lots')).toBeNull();
    expect(kvToGb(undefined)).toBeNull();
    expect(kvToGb(0)).toBeNull();
  });

  it('formats sizes, tokens and times, and dashes what is missing', () => {
    expect(formatGb(4)).toBe('4 GB');
    expect(formatGb(3.456)).toBe('3.5 GB');
    expect(formatGb(null)).toBe('–');
    expect(mbToGb(2048)).toBe(2);
    expect(mbToGb(null)).toBeNull();
    expect(formatTokens(32768)).toBe('32,768');
    expect(formatTokens(undefined)).toBe('–');
    expect(formatSeconds(54.64)).toBe('54.6 s');
    expect(formatSeconds(null)).toBe('–');
  });

  it('describes a placement from whatever keys are set', () => {
    expect(
      describePlacement({ tensor_parallel_size: 4, kv_cache_memory_bytes: '3G', max_model_len: 32768 }),
    ).toBe('TP 4 · KV 3 GB per GPU · context 32768');
    expect(describePlacement({ tensor_parallel_size: 2 })).toBe('TP 2');
    expect(describePlacement({})).toBe('');
  });

  it('reads capability and overrides of an entry as one set, capability first', () => {
    const entry = {
      model_name: 'org/a',
      capability: { tensor_parallel_size: 2, min_context_fraction: 0.5 },
      overrides: { tensor_parallel_size: 1, quantization: 'awq', chat_template_kwargs: { a: 1 } },
    };
    expect(entrySettings(entry)).toMatchObject({ tensor_parallel_size: 2, quantization: 'awq' });
    expect(entrySettings(undefined)).toEqual({});
    expect(advancedSettings(entry)).toEqual([
      { key: 'chat_template_kwargs', value: '{"a":1}' },
      { key: 'min_context_fraction', value: '0.5' },
      { key: 'quantization', value: 'awq' },
    ]);
  });
});
