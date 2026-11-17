import { describe, expect, it } from 'vitest';

import { CalibrationPair, CalibrationResult } from '../../../shared/models/node-config.model';
import { contextCapacity, kvWindowGb, saveFailure, withPlacement, withoutEntry } from './node-model-edit';

const result = (min: number | null, max: number | null): CalibrationResult => ({
  tp: 2,
  active: true,
  base_residency_mb: null,
  loaded_vram_mb: null,
  kv_min_mb: min,
  kv_max_mb: max,
  pairs: [],
  cold_load_time_s: null,
  wake_from_sleep_time_s: null,
  enforce_eager: null,
});

const pairs: CalibrationPair[] = [
  { kv_mb: 4096, max_model_len: 32768, measured: true },
  { kv_mb: 1024, max_model_len: 5440, measured: true },
];

describe('the KV window', () => {
  it('takes whole GB inside the measured range', () => {
    expect(kvWindowGb(result(1024, 4096))).toEqual({ min: 1, max: 4 });
    expect(kvWindowGb(result(1500, 4600))).toEqual({ min: 2, max: 4 });
  });

  it('is none without a measurement or without a whole GB in it', () => {
    expect(kvWindowGb(null)).toBeNull();
    expect(kvWindowGb(result(null, 4096))).toBeNull();
    expect(kvWindowGb(result(1100, 1900))).toBeNull();
  });
});

describe('the context capacity', () => {
  it('follows the measured curve between its points', () => {
    expect(contextCapacity(pairs, 4, null)).toBe(32768);
    expect(contextCapacity(pairs, 1, null)).toBe(5120);
    // 2.5 GB lies between 1 GB (5,440) and 4 GB (32,768): 19,104 in steps.
    expect(contextCapacity(pairs, 2.5, null)).toBe(18432);
  });

  it('scales below the first point and stays at the last above it', () => {
    expect(contextCapacity(pairs, 0.5, null)).toBe(2048);
    expect(contextCapacity(pairs, 8, null)).toBe(32768);
  });

  it('never goes below one step and never above the context limit of the model', () => {
    expect(contextCapacity(pairs, 0.01, null)).toBe(1024);
    expect(contextCapacity(pairs, 4, 16384)).toBe(16384);
  });

  it('falls back to the limit when nothing was measured', () => {
    expect(contextCapacity([], 4, 8192)).toBe(8192);
    expect(contextCapacity([], 4, null)).toBeNull();
    expect(contextCapacity([{ kv_mb: null, max_model_len: 5, measured: true }], 4, 100)).toBe(100);
  });
});

describe('writing a placement', () => {
  const other = { model_name: 'org/b', capability: {}, overrides: { quantization: 'awq' } };
  const mine = {
    model_name: 'org/a',
    capability: { tensor_parallel_size: 1, min_context_fraction: 1 },
    overrides: { tensor_parallel_size: 1, kv_cache_memory_bytes: '2G', max_model_len: 1024, quantization: 'awq' },
  };

  it('replaces only the entry of the model and keeps its other settings', () => {
    const entries = withPlacement([other, mine], 'org/a', { tp: 2, kvGb: 4, ctx: 32768 });

    expect(entries[0]).toBe(other);
    expect(entries[1]).toEqual({
      model_name: 'org/a',
      capability: { tensor_parallel_size: 2, kv_cache_memory_bytes: '4G', min_context_fraction: 1 },
      overrides: { max_model_len: 32768, quantization: 'awq' },
    });
  });

  it('appends an entry for a model that has none', () => {
    const entries = withPlacement([other], 'org/new', { tp: 1, kvGb: 3, ctx: 4096 });

    expect(entries).toHaveLength(2);
    expect(entries[1]).toEqual({
      model_name: 'org/new',
      capability: { tensor_parallel_size: 1, kv_cache_memory_bytes: '3G' },
      overrides: { max_model_len: 4096 },
    });
  });
});

describe('removing an entry', () => {
  it('leaves the others as they are', () => {
    const a = { model_name: 'org/a', capability: {}, overrides: {} };
    const b = { model_name: 'org/b', capability: {}, overrides: {} };

    expect(withoutEntry([a, b], 'org/a')).toEqual([b]);
    expect(withoutEntry([a], 'org/other')).toEqual([a]);
  });
});

describe('what a failed save means', () => {
  it('tells a conflict, a rejection, an unreachable worker and anything else apart', () => {
    expect(saveFailure({ status: 409, error: { error: 'revision 5' } })).toEqual({
      kind: 'conflict',
      message: 'revision 5',
      items: [],
    });
    const items = [{ model: 'org/a', key: 'max_model_len', message: 'bad', suggestion: null }];
    expect(saveFailure({ status: 400, error: { error: 'org/a: max_model_len: bad', errors: items } })).toEqual({
      kind: 'rejected',
      message: 'org/a: max_model_len: bad',
      items,
    });
    expect(saveFailure({ status: 503, error: { error: 'Worker not connected' } }).kind).toBe('unreachable');
    expect(saveFailure({ status: 502 }).kind).toBe('unreachable');
    expect(saveFailure({ status: 0 }).kind).toBe('unreachable');
    expect(saveFailure({ status: 500, error: { error: 'boom' } })).toMatchObject({ kind: 'other', message: 'boom' });
  });

  it('keeps a rejection without a list readable and answers unknown errors in general words', () => {
    expect(saveFailure({ status: 400, error: { error: 'x' } }).items).toEqual([]);
    expect(saveFailure(undefined).message).toBe('The configuration could not be saved.');
  });
});
