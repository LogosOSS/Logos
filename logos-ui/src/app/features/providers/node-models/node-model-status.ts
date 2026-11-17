import { NodeModelInsight } from '../../../shared/models/node-config.model';

export type StatusTone = 'ok' | 'gray' | 'warn' | 'err';

export interface CalibrationLabel {
  text: string;
  tone: StatusTone;
  title: string | null;
}

/** Settings of a lane or a benchmark run that decide where a model sits. */
export const PLACEMENT_KEYS = ['tensor_parallel_size', 'kv_cache_memory_bytes', 'max_model_len'] as const;

/** A comparable benchmark run placed the model differently from now. */
export function hasTuning(model: NodeModelInsight): boolean {
  const best = model.benchmark?.best_eligible;
  if (!best || !model.benchmark?.eligible_for_comparison) {
    return false;
  }
  const current = model.benchmark.current;
  return PLACEMENT_KEYS.some((key) => key in best.settings && String(best.settings[key]) !== String(current[key]));
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
