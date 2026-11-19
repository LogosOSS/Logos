import { NodeConfigEntry } from '../../../shared/models/node-config.model';

const UNITS_GB: Record<string, number> = { K: 1 / 1024 / 1024, M: 1 / 1024, G: 1, T: 1024 };

/** A KV cache size ("4G", "4096M", bytes) in GB; null if it is no size. */
export function kvToGb(value: unknown): number | null {
  if (typeof value === 'number') {
    // A number is bytes, like a bare numeric string and like the field name says.
    return Number.isFinite(value) && value > 0 ? value / 1024 ** 3 : null;
  }
  const match = /^(\d+(?:\.\d+)?)\s*([KMGT])?B?$/i.exec(String(value ?? '').trim());
  if (!match) {
    return null;
  }
  const amount = Number(match[1]);
  if (!match[2]) {
    // A bare number is bytes, as vLLM reads it.
    return amount / 1024 ** 3;
  }
  return amount * UNITS_GB[match[2].toUpperCase()];
}

export function formatGb(gb: number | null | undefined): string {
  if (gb === null || gb === undefined || !Number.isFinite(gb)) {
    return '–';
  }
  return `${Number.isInteger(gb) ? gb : gb.toFixed(1)} GB`;
}

export function mbToGb(mb: number | null | undefined): number | null {
  return typeof mb === 'number' && Number.isFinite(mb) ? mb / 1024 : null;
}

export function formatTokens(tokens: unknown): string {
  const n = Number(tokens);
  return Number.isFinite(n) && n > 0 ? n.toLocaleString('en-US') : '–';
}

export function formatSeconds(seconds: number | null | undefined): string {
  return typeof seconds === 'number' && Number.isFinite(seconds) ? `${Math.round(seconds * 10) / 10} s` : '–';
}

/** "TP 4 · KV 3 GB per GPU · context 32768", from the placement keys. */
export function describePlacement(settings: Record<string, unknown>): string {
  const parts: string[] = [];
  if (settings['tensor_parallel_size'] != null) {
    parts.push(`TP ${settings['tensor_parallel_size']}`);
  }
  const kv = kvToGb(settings['kv_cache_memory_bytes']);
  if (kv !== null) {
    parts.push(`KV ${formatGb(kv)} per GPU`);
  }
  if (settings['max_model_len'] != null) {
    parts.push(`context ${settings['max_model_len']}`);
  }
  return parts.join(' · ');
}

/** A stored entry: capability and overrides read as one set of settings. */
export function entrySettings(entry: NodeConfigEntry | undefined): Record<string, unknown> {
  return { ...(entry?.overrides ?? {}), ...(entry?.capability ?? {}) };
}

/** What the entry sets besides TP, KV cache and context, as text per key. */
export function advancedSettings(entry: NodeConfigEntry | undefined): { key: string; value: string }[] {
  const placed = new Set(['tensor_parallel_size', 'kv_cache_memory_bytes', 'max_model_len']);
  return Object.entries(entrySettings(entry))
    .filter(([key]) => !placed.has(key))
    .map(([key, value]) => ({
      key,
      value: typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value),
    }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

/** TP, KV cache and context of a placement as figures for the tiles of a page. */
export function placementFigures(values: Record<string, unknown> | null | undefined): { value: string; caption: string }[] {
  if (!values) {
    return [];
  }
  const out: { value: string; caption: string }[] = [];
  const tp = Number(values['tensor_parallel_size']);
  if (tp > 0) {
    out.push({ value: `TP ${tp}`, caption: tp === 1 ? 'Tensor parallel · 1 GPU' : `Tensor parallel · ${tp} GPUs` });
  }
  const kv = kvToGb(values['kv_cache_memory_bytes']);
  if (kv !== null) {
    out.push({ value: formatGb(kv), caption: 'KV cache per GPU' });
  }
  const ctx = Number(values['max_model_len']);
  if (ctx > 0) {
    out.push({ value: `${formatTokens(ctx)} tokens`, caption: 'Max. context length' });
  }
  return out;
}
