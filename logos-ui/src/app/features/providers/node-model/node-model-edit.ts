import { CalibrationPair, CalibrationResult, NodeConfigEntry } from '../../../shared/models/node-config.model';
import { AdvancedDraft, applyAdvanced } from './node-model-advanced';

const CONTEXT_STEP = 1024;

export interface Placement {
  tp: number;
  kvGb: number;
  ctx: number;
}

export interface ErrorItem {
  model: string | null;
  key: string | null;
  message: string;
  suggestion: string | null;
}

export type FailureKind = 'conflict' | 'rejected' | 'unreachable' | 'other';

export interface SaveFailure {
  kind: FailureKind;
  message: string;
  items: ErrorItem[];
}

/** Whole GB the KV slider may take, inside the measured window. */
export function kvWindowGb(result: CalibrationResult | null): { min: number; max: number } | null {
  if (!result || result.kv_min_mb == null || result.kv_max_mb == null) {
    return null;
  }
  const min = Math.max(1, Math.ceil(result.kv_min_mb / 1024));
  const max = Math.floor(result.kv_max_mb / 1024);
  return max >= min ? { min, max } : null;
}

/** The longest context the curve allows at this KV size, in steps of 1,024. */
export function contextCapacity(pairs: CalibrationPair[], kvGb: number, limit: number | null | undefined): number | null {
  const points = pairs
    .filter((p) => p.kv_mb != null && p.max_model_len != null)
    .map((p) => ({ kv: p.kv_mb as number, len: p.max_model_len as number }))
    .sort((a, b) => a.kv - b.kv);
  if (points.length === 0) {
    return limit ?? null;
  }
  const kv = kvGb * 1024;
  let len: number;
  if (kv <= points[0].kv) {
    len = (points[0].len / points[0].kv) * kv;
  } else if (kv >= points[points.length - 1].kv) {
    len = points[points.length - 1].len;
  } else {
    const upper = points.findIndex((p) => p.kv >= kv);
    const a = points[upper - 1];
    const b = points[upper];
    len = a.len + ((b.len - a.len) * (kv - a.kv)) / (b.kv - a.kv);
  }
  const capped = limit ? Math.min(len, limit) : len;
  return Math.max(CONTEXT_STEP, Math.floor(capped / CONTEXT_STEP) * CONTEXT_STEP);
}

/** The list with one model's placement written as the orchestrator expects. */
export function withPlacement(
  entries: NodeConfigEntry[],
  model: string,
  placement: Placement,
  advanced?: AdvancedDraft,
): NodeConfigEntry[] {
  const placed = (entry: NodeConfigEntry): NodeConfigEntry => {
    const overrides = { ...entry.overrides };
    // TP and KV cache live in capability; a copy in overrides would conflict.
    delete overrides['tensor_parallel_size'];
    delete overrides['kv_cache_memory_bytes'];
    overrides['max_model_len'] = placement.ctx;
    const result = {
      ...entry,
      capability: {
        ...entry.capability,
        tensor_parallel_size: placement.tp,
        kv_cache_memory_bytes: `${placement.kvGb}G`,
      },
      overrides,
    };
    return advanced ? applyAdvanced(result, advanced) : result;
  };
  if (entries.some((e) => e.model_name === model)) {
    return entries.map((e) => (e.model_name === model ? placed(e) : e));
  }
  return [...entries, placed({ model_name: model, capability: {}, overrides: {} })];
}

/** The list without the entry of one model. */
export function withoutEntry(entries: NodeConfigEntry[], model: string): NodeConfigEntry[] {
  return entries.filter((e) => e.model_name !== model);
}

/** What a failed save means for the editor, from the HTTP answer of Logos. */
export function saveFailure(e: unknown): SaveFailure {
  const response = (e as { status?: number; error?: { error?: string; errors?: ErrorItem[] }; message?: string }) ?? {};
  const text = response.error?.error || response.message || 'The configuration could not be saved.';
  switch (response.status) {
    case 409:
      return { kind: 'conflict', message: text, items: [] };
    case 400:
      return { kind: 'rejected', message: text, items: response.error?.errors ?? [] };
    case 0:
    case 502:
    case 503:
      return { kind: 'unreachable', message: text, items: [] };
    default:
      return { kind: 'other', message: text, items: [] };
  }
}
