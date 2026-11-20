import { describe, expect, it } from 'vitest';

import {
  advancedSettings,
  BATCHING_FLAGS,
  flagValueOf,
  describePlacement,
  describeRecommendation,
  entrySettings,
  formatGb,
  formatSeconds,
  formatTokens,
  kvToGb,
  mbToGb,
  storedSettings,
  withFlag,
} from './node-model-format';

describe('node model formatting', () => {
  it('reads KV cache sizes with and without a unit', () => {
    expect(kvToGb('4G')).toBe(4);
    expect(kvToGb('4096M')).toBe(4);
    expect(kvToGb('2.5g')).toBe(2.5);
    expect(kvToGb('4194304K')).toBe(4);
    expect(kvToGb('1T')).toBe(1024);
    expect(kvToGb('4294967296')).toBe(4);
    expect(kvToGb(4294967296)).toBe(4);
    expect(kvToGb(3)).toBeCloseTo(0, 6);
    expect(kvToGb(Number.NaN)).toBeNull();
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

describe('the batch flags of an entry', () => {
  it('reads either spelling of a flag from the extra arguments', () => {
    expect(flagValueOf(['--max-num-batched-tokens=2048', '--foo'], '--max-num-batched-tokens')).toBe('2048');
    expect(flagValueOf(['--foo', '--max-num-seqs', '8'], '--max-num-seqs')).toBe('8');
    expect(flagValueOf(['--foo'], '--max-num-seqs')).toBeNull();
    expect(flagValueOf(undefined, '--max-num-seqs')).toBeNull();
    // The launch command gives the last occurrence precedence.
    expect(flagValueOf(['--max-num-seqs=64', '--max-num-seqs=8'], '--max-num-seqs')).toBe('8');
    expect(flagValueOf(['--max-num-seqs', '64', '--max-num-seqs', '8'], '--max-num-seqs')).toBe('8');
    expect(flagValueOf(['--max-num-seqs=64', '--max-num-seqs', '8'], '--max-num-seqs')).toBe('8');
  });

  it('are part of the stored settings, and a flag in the extra arguments is the value in force', () => {
    const entry = {
      model_name: 'm',
      capability: {},
      overrides: { max_num_seqs: 64, extra_args: ['--max-num-batched-tokens=2048', '--max-num-seqs=8'] },
    };

    expect(storedSettings(entry as never)).toMatchObject({ max_num_seqs: '8', max_num_batched_tokens: '2048' });
    expect(storedSettings({ model_name: 'm', capability: {}, overrides: { max_num_seqs: 64 } } as never)['max_num_seqs']).toBe(64);
    expect(storedSettings({ model_name: 'm', capability: {}, overrides: {} } as never)['max_num_batched_tokens']).toBeUndefined();
  });

  it('are set once and last in the lines of the extra arguments, whatever spelling was there', () => {
    const tokens = BATCHING_FLAGS['max_num_batched_tokens'];
    expect(withFlag('--foo\n--max-num-batched-tokens=2048', tokens, 4096)).toBe('--foo\n--max-num-batched-tokens=4096');
    expect(withFlag('--max-num-batched-tokens\n2048\n--foo', tokens, '4096')).toBe('--foo\n--max-num-batched-tokens=4096');
    expect(withFlag('--max-num-seqs=8\n--foo', BATCHING_FLAGS['max_num_seqs'], 64)).toBe('--foo\n--max-num-seqs=64');
    expect(withFlag('', tokens, 4096)).toBe('--max-num-batched-tokens=4096');
  });

  it('is named with the placement of a recommendation', () => {
    expect(describeRecommendation({ tensor_parallel_size: 1, max_num_seqs: 64, max_num_batched_tokens: 4096 })).toBe(
      'TP 1 · 64 parallel requests · 4096 batched tokens',
    );
    expect(describeRecommendation({ max_num_seqs: 64 })).toBe('64 parallel requests');
  });
});
