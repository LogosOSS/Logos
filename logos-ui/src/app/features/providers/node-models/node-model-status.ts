import { NodeModelInsight } from '../../../shared/models/node-config.model';
import { BATCHING_KEYS, kvToGb } from '../node-model/node-model-format';

export type StatusTone = 'ok' | 'gray' | 'warn' | 'err';

export interface CalibrationLabel {
  text: string;
  tone: StatusTone;
  title: string | null;
}

/** Settings of a lane or a benchmark run that decide where a model sits. */
export const PLACEMENT_KEYS = ['tensor_parallel_size', 'kv_cache_memory_bytes', 'max_model_len'] as const;

/** A recommendation and a configuration agree on a key; a cache size is compared as a size. */
function sameValue(key: string, a: unknown, b: unknown): boolean {
  if (key === 'kv_cache_memory_bytes') {
    const x = kvToGb(a);
    const y = kvToGb(b);
    return x !== null && y !== null ? Math.abs(x - y) < 0.05 : String(a) === String(b);
  }
  return String(a) === String(b);
}

/**
 * A comparable benchmark run recommends other values than the stored configuration sets.
 *
 * `stored` is what the stored entry sets (`storedSettings`). A placement value the entry leaves open is the
 * worker's choice, so it is compared with what the lane runs. A batch value the entry leaves open counts as different.
 */
export function hasTuning(model: NodeModelInsight, stored: Record<string, unknown> = {}): boolean {
  const best = model.benchmark?.best_eligible;
  if (!best || !model.benchmark?.eligible_for_comparison) {
    return false;
  }
  const current = model.benchmark.current;
  const differs = (key: string, now: unknown) => key in best.settings && !sameValue(key, best.settings[key], now);
  return (
    PLACEMENT_KEYS.some((key) => differs(key, key in stored ? stored[key] : current[key])) ||
    BATCHING_KEYS.some((key) => differs(key, stored[key]))
  );
}

/** Calibration status of a model. */
export function calibrationLabel(model: NodeModelInsight): CalibrationLabel {
  const { status, reason } = model.calibration;
  if (status === 'unsupported') {
    return { text: 'Unsupported', tone: 'err', title: reason };
  }
  if (status === 'calibrated') {
    return { text: 'Calibrated', tone: 'ok', title: null };
  }
  if (model.calibrating) {
    return { text: 'Calibrating…', tone: 'warn', title: null };
  }
  if (status === 'outdated') {
    return { text: 'Calibration outdated', tone: 'gray', title: null };
  }
  return { text: 'Not calibrated', tone: 'gray', title: null };
}

export interface PlacementLabel {
  text: string;
  tone: StatusTone;
  title: string | null;
}

/**
 * Where a model sits, in the words of the list.
 *
 * The worker loads a model only with a calibration, so a model without one is not loadable, whatever its
 * placement: "Automatic" would promise a choice that the worker cannot make.
 */
export function placementLabel(model: NodeModelInsight): PlacementLabel {
  const { status, reason } = model.calibration;
  if (status === 'unsupported') {
    return { text: 'Not loadable', tone: 'err', title: reason };
  }
  if (status === 'not_calibrated') {
    return { text: 'Not loadable', tone: 'gray', title: 'The model needs a calibration before the worker can load it.' };
  }
  if (model.configured) {
    return { text: 'Set', tone: 'ok', title: null };
  }
  if (status === 'outdated') {
    return {
      text: 'Automatic',
      tone: 'warn',
      title: 'The calibration is outdated. The model keeps running until it is measured again.',
    };
  }
  return { text: 'Automatic', tone: 'gray', title: 'The worker chooses TP, KV cache and context itself.' };
}
