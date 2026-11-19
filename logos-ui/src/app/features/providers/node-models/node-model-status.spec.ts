import { describe, expect, it } from 'vitest';

import { NodeModelInsight } from '../../../shared/models/node-config.model';
import { hasTuning } from './node-model-status';

const model = (best: Record<string, unknown> | null, current: Record<string, unknown> = {}, comparable = true) =>
  ({
    model: 'org/m',
    benchmark: best
      ? {
          best_eligible: { benchmark_id: 2, throughput: 50, settings: best, recorded_at: null },
          best_observed: null,
          current,
          eligible_for_comparison: comparable,
        }
      : null,
  }) as unknown as NodeModelInsight;

describe('a recommendation of a benchmark', () => {
  it('is offered for a placement the lane does not run when the entry leaves it open', () => {
    const current = { tensor_parallel_size: 2, max_model_len: 8192 };

    expect(hasTuning(model({ tensor_parallel_size: 4 }, current))).toBe(true);
    expect(hasTuning(model({ tensor_parallel_size: 2, max_model_len: 8192 }, current))).toBe(false);
  });

  it('compares a placement with the stored entry when the entry sets it', () => {
    const current = { tensor_parallel_size: 2 };

    expect(hasTuning(model({ tensor_parallel_size: 2 }, current), { tensor_parallel_size: 1 })).toBe(true);
    expect(hasTuning(model({ tensor_parallel_size: 2 }, current), { tensor_parallel_size: 2 })).toBe(false);
  });

  it('compares a cache size as a size, not as text', () => {
    expect(hasTuning(model({ kv_cache_memory_bytes: 7516192768 }), { kv_cache_memory_bytes: '7G' })).toBe(false);
    expect(hasTuning(model({ kv_cache_memory_bytes: 7516192768 }), { kv_cache_memory_bytes: '4G' })).toBe(true);
  });

  it('is offered for batch values the stored entry does not set or sets differently', () => {
    // The lane already runs the winner, the stored entry does not: this is the case of a nightly benchmark.
    const current = { max_num_seqs: 64, max_num_batched_tokens: 4096 };
    const best = { max_num_seqs: 64, max_num_batched_tokens: 4096 };

    expect(hasTuning(model(best, current))).toBe(true);
    expect(hasTuning(model(best, current), { max_num_seqs: 32, max_num_batched_tokens: 4096 })).toBe(true);
    expect(hasTuning(model(best, current), { max_num_seqs: 64, max_num_batched_tokens: 2048 })).toBe(true);
    expect(hasTuning(model(best, current), { max_num_seqs: 64, max_num_batched_tokens: 4096 })).toBe(false);
  });

  it('counts a batch flag of the stored extra arguments as the value in force', () => {
    const best = { max_num_seqs: 64 };

    // The dedicated value is already 64, but the flag after it makes vLLM run with 8.
    expect(hasTuning(model(best), { max_num_seqs: '8' })).toBe(true);
    expect(hasTuning(model(best), { max_num_seqs: '64' })).toBe(false);
  });

  it('is not offered without a comparable run or for a setting it does not apply', () => {
    expect(hasTuning(model({ tensor_parallel_size: 4 }, {}, false))).toBe(false);
    expect(hasTuning(model({ block_size: 16 }))).toBe(false);
    expect(hasTuning(model(null))).toBe(false);
  });
});
