import { NodeConfigEntry } from '../../../shared/models/node-config.model';
import { entrySettings } from './node-model-format';

export type FieldKind = 'text' | 'int' | 'num' | 'tri' | 'percent' | 'lines' | 'select' | 'gpus';

export type GroupId = 'placement' | 'engine' | 'memory' | 'performance' | 'serving' | 'arguments';

export interface FieldGroup {
  id: GroupId;
  title: string;
  /** Shown at once; the others sit under "All engine settings". */
  top: boolean;
}

export const FIELD_GROUPS: FieldGroup[] = [
  { id: 'placement', title: 'Placement', top: true },
  { id: 'engine', title: 'Engine', top: true },
  { id: 'memory', title: 'Memory and precision', top: false },
  { id: 'performance', title: 'Performance and stability', top: false },
  { id: 'serving', title: 'Serving', top: false },
  { id: 'arguments', title: 'Arguments', top: false },
];

export interface FieldOption {
  value: string;
  label: string;
}

export interface AdvancedField {
  key: string;
  label: string;
  kind: FieldKind;
  group: GroupId;
  where: 'overrides' | 'capability';
  help: string;
  placeholder?: string;
  min?: number;
  max?: number;
  unit?: string;
  options?: FieldOption[];
}

export interface CustomRow {
  key: string;
  value: string;
}

/** The settings besides placement, as text per field; empty means unset. */
export interface AdvancedDraft {
  values: Record<string, string>;
  custom: CustomRow[];
}

/** The choices of a select field: the default first, then each item; a plain value is its own label. */
const choices = (...items: (string | FieldOption)[]): FieldOption[] => [
  { value: '', label: 'Default' },
  ...items.map((item) => (typeof item === 'string' ? { value: item, label: item } : item)),
];

type FieldSpec = Omit<AdvancedField, 'group' | 'where'>;

/** The fields of one group, which all keep their value in the same part of an entry. */
const fieldsOf = (group: GroupId, where: AdvancedField['where'], specs: FieldSpec[]): AdvancedField[] =>
  specs.map((spec) => ({ ...spec, group, where }));

export const ADVANCED_FIELDS: AdvancedField[] = [
  ...fieldsOf('placement', 'capability', [
    {
      key: 'gpu_devices',
      label: 'GPU devices',
      kind: 'gpus',
      help: 'The GPUs this model may use. All of them by default.',
    },
    {
      key: 'min_context_fraction',
      label: 'Minimum context share',
      kind: 'percent',
      min: 0,
      max: 100,
      unit: '%',
      help: 'The planner only places this model where it can offer at least this share of its context window. 100 % means full context or nothing.',
    },
  ]),
  ...fieldsOf('engine', 'overrides', [
    {
      key: 'max_num_seqs',
      label: 'Max. parallel requests',
      kind: 'int',
      min: 1,
      placeholder: 'Default',
      help: 'Requests the engine processes at the same time. Empty lets vLLM or the calibrated profile decide.',
    },
    {
      key: 'enable_prefix_caching',
      label: 'Prefix caching',
      kind: 'tri',
      help: 'Reuse the KV cache of shared prompt beginnings.',
    },
  ]),
  ...fieldsOf('memory', 'overrides', [
    {
      key: 'gpu_memory_utilization',
      label: 'GPU memory fraction',
      kind: 'num',
      min: 0.1,
      max: 1,
      placeholder: 'Auto (from calibration)',
      help: 'Share of each GPU vLLM may use. Empty derives it from calibration.',
    },
    {
      key: 'dtype',
      label: 'Compute precision',
      kind: 'select',
      options: choices({ value: 'auto', label: 'Auto' }, { value: 'half', label: 'half (float16)' }, 'bfloat16', 'float32'),
      help: 'Data type of the weights and activations.',
    },
    {
      key: 'quantization',
      label: 'Quantization',
      kind: 'text',
      placeholder: 'From the model',
      help: 'Must match the model weights. Usually detected automatically.',
    },
    {
      key: 'kv_cache_dtype',
      label: 'KV cache data type',
      kind: 'select',
      options: choices({ value: 'auto', label: 'Auto' }, 'fp8', 'fp8_e4m3', 'fp8_e5m2'),
      help: 'fp8 shrinks the KV cache on supported GPUs.',
    },
    {
      key: 'cpu_offload_gb',
      label: 'CPU offload',
      kind: 'num',
      min: 0,
      max: 1024,
      unit: 'GB',
      placeholder: '0 (off)',
      help: 'CPU memory used to offload weights. Slower, but fits larger models.',
    },
    {
      key: 'mm_processor_cache_gb',
      label: 'Multimodal cache',
      kind: 'num',
      min: 0,
      max: 1024,
      unit: 'GB',
      placeholder: 'Default',
      help: 'Cache for image and audio inputs. 0 turns it off.',
    },
  ]),
  ...fieldsOf('performance', 'overrides', [
    {
      key: 'enforce_eager',
      label: 'Eager mode',
      kind: 'tri',
      help: 'Skip CUDA graph capture. Slower, but avoids graph memory overhead.',
    },
    {
      key: 'attention_backend',
      label: 'Attention backend',
      kind: 'select',
      options: choices('FLASHINFER', 'FLASH_ATTN', 'TRITON_ATTN', 'FLEX_ATTENTION'),
      help: 'The attention kernel. Empty lets vLLM choose.',
    },
    {
      key: 'cuda_graph_sizes',
      label: 'CUDA graph sizes',
      kind: 'text',
      placeholder: 'vLLM default, e.g. 1,2,4,8',
      help: 'Batch sizes to capture, comma separated. Ignored in eager mode.',
    },
    {
      key: 'disable_custom_all_reduce',
      label: 'Disable custom all-reduce',
      kind: 'tri',
      help: 'On turns the custom all-reduce kernel off. Use it when multi-GPU runs hang.',
    },
  ]),
  ...fieldsOf('serving', 'overrides', [
    {
      key: 'chat_template',
      label: 'Chat template',
      kind: 'text',
      placeholder: 'From the model',
      help: 'A file name in the chat-templates folder of the worker.',
    },
    {
      key: 'tool_call_parser',
      label: 'Tool call parser',
      kind: 'text',
      placeholder: 'None',
      help: 'The parser for tool calls, for example hermes.',
    },
    {
      key: 'reasoning_parser',
      label: 'Reasoning parser',
      kind: 'text',
      placeholder: 'None',
      help: 'The parser that separates reasoning from the answer.',
    },
    {
      key: 'enable_auto_tool_choice',
      label: 'Automatic tool choice',
      kind: 'tri',
      help: 'Let the model decide when to call a tool.',
    },
  ]),
  ...fieldsOf('arguments', 'overrides', [
    {
      key: 'extra_args',
      label: 'Extra arguments',
      kind: 'lines',
      placeholder: '--max-num-batched-tokens=2048',
      help: 'Passed to vllm serve, one per line.',
    },
  ]),
];

const FIELD_KEYS = new Set(ADVANCED_FIELDS.map((f) => f.key));
const PLACEMENT_KEYS = new Set(['tensor_parallel_size', 'kv_cache_memory_bytes', 'max_model_len']);
const KEY_PATTERN = /^[a-z_][a-z0-9_]*$/;

const textOf = (value: unknown): string => {
  if (value === undefined || value === null) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
};

/** The settings an entry sets besides placement, as the editor shows them. */
export function initialAdvanced(entry: NodeConfigEntry | undefined): AdvancedDraft {
  const settings = entrySettings(entry);
  const values: Record<string, string> = {};
  for (const field of ADVANCED_FIELDS) {
    const raw = settings[field.key];
    if (raw === undefined || raw === null) {
      values[field.key] = '';
    } else if (field.kind === 'percent') {
      values[field.key] = String(Math.round(Number(raw) * 100));
    } else if (field.kind === 'lines' && Array.isArray(raw)) {
      values[field.key] = raw.map(String).join('\n');
    } else {
      values[field.key] = textOf(raw);
    }
  }
  const custom = Object.entries(entry?.overrides ?? {})
    .filter(([key]) => !FIELD_KEYS.has(key) && !PLACEMENT_KEYS.has(key))
    .map(([key, value]) => ({ key, value: textOf(value) }))
    .sort((a, b) => a.key.localeCompare(b.key));
  return { values, custom };
}

/** Problems that stop a save, by field key or "custom:<row>". */
export function advancedErrors(draft: AdvancedDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const field of ADVANCED_FIELDS) {
    const text = (draft.values[field.key] ?? '').trim();
    if (text === '') continue;
    const n = Number(text);
    if (field.kind === 'int' && !(/^\d+$/.test(text) && n >= (field.min ?? 0))) {
      errors[field.key] = `Use a whole number of ${field.min ?? 0} or more.`;
    } else if ((field.kind === 'num' || field.kind === 'percent') && !(Number.isFinite(n) && n >= field.min! && n <= field.max!)) {
      errors[field.key] = `Use a value from ${field.min} to ${field.max}.`;
    } else if (['quantization', 'tool_call_parser', 'reasoning_parser'].includes(field.key) && !/^[a-zA-Z0-9_.-]*$/.test(text)) {
      errors[field.key] = 'Use letters, digits, dots, dashes and underscores only.';
    } else if (field.key === 'cuda_graph_sizes' && !/^\d+(,\d+)*$/.test(text)) {
      errors[field.key] = 'Use whole numbers separated by commas, like 1,2,4,8.';
    } else if (field.kind === 'gpus' && !/^\d+(,\d+)*$/.test(text)) {
      errors[field.key] = 'Use GPU numbers separated by commas, like 0,1.';
    } else if (field.key === 'chat_template' && text.includes('..')) {
      errors[field.key] = 'A file name cannot contain "..".';
    }
  }
  const seen = new Set<string>();
  draft.custom.forEach((row, index) => {
    const key = row.key.trim();
    if (key === '') return;
    if (!KEY_PATTERN.test(key)) {
      errors[`custom:${index}`] = 'Use lowercase letters, digits and underscores.';
    } else if (FIELD_KEYS.has(key) || PLACEMENT_KEYS.has(key)) {
      errors[`custom:${index}`] = 'This setting has its own field.';
    } else if (seen.has(key)) {
      errors[`custom:${index}`] = 'This setting is listed twice.';
    }
    seen.add(key);
  });
  return errors;
}

const parseCustom = (value: string): unknown => {
  const text = value.trim();
  if (text === 'true') return true;
  if (text === 'false') return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text.startsWith('{') || text.startsWith('[')) {
    try {
      return JSON.parse(text);
    } catch {
      return value;
    }
  }
  return value;
};

const parseField = (field: AdvancedField, text: string): unknown => {
  switch (field.kind) {
    case 'int':
    case 'num':
      return Number(text);
    case 'percent':
      return Number(text) / 100;
    case 'tri':
      return text === 'true';
    case 'lines':
      return text.split('\n').map((l) => l.trim()).filter((l) => l !== '');
    default:
      return text;
  }
};

/** The entry with the settings of the draft; an empty field removes its key. */
export function applyAdvanced(entry: NodeConfigEntry, draft: AdvancedDraft): NodeConfigEntry {
  const overrides: Record<string, unknown> = {};
  const capability = { ...entry.capability };
  for (const [key, value] of Object.entries(entry.overrides)) {
    if (PLACEMENT_KEYS.has(key)) overrides[key] = value;
  }
  for (const field of ADVANCED_FIELDS) {
    const target = field.where === 'capability' ? capability : overrides;
    // A copy under the other name (an imported file may hold one) would win on the next read.
    delete capability[field.key];
    delete target[field.key];
    const text = (draft.values[field.key] ?? '').trim();
    if (text === '') continue;
    const parsed = parseField(field, field.kind === 'lines' ? draft.values[field.key] : text);
    if (!(Array.isArray(parsed) && parsed.length === 0)) {
      target[field.key] = parsed;
    }
  }
  for (const row of draft.custom) {
    const key = row.key.trim();
    if (key !== '') overrides[key] = parseCustom(row.value);
  }
  return { ...entry, capability, overrides };
}

/** Settings that differ from where the editor started, for the save bar. */
export function advancedChanges(start: AdvancedDraft, now: AdvancedDraft): string[] {
  const out: string[] = [];
  for (const field of ADVANCED_FIELDS) {
    const a = (start.values[field.key] ?? '').trim();
    const b = (now.values[field.key] ?? '').trim();
    if (a !== b) {
      out.push(`${field.label}: ${a === '' ? 'default' : a.replace(/\n/g, ' ')} → ${b === '' ? 'default' : b.replace(/\n/g, ' ')}`);
    }
  }
  const serial = (d: AdvancedDraft) => JSON.stringify(d.custom.filter((r) => r.key.trim() !== '').map((r) => [r.key.trim(), r.value.trim()]));
  if (serial(start) !== serial(now)) {
    out.push('Custom settings changed');
  }
  return out;
}
