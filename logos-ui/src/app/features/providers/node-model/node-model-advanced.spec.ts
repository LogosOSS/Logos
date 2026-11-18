import { describe, expect, it } from 'vitest';

import {
  advancedChanges,
  advancedErrors,
  applyAdvanced,
  initialAdvanced,
} from './node-model-advanced';

const entry = {
  model_name: 'org/a',
  capability: { tensor_parallel_size: 4, kv_cache_memory_bytes: '4G', min_context_fraction: 0.5, base_residency_mb: 900 },
  overrides: {
    max_model_len: 32768,
    quantization: 'awq',
    enforce_eager: true,
    extra_args: ['--a=1', '--b'],
    custom_flag: 'x',
    swap_space: 'TRITON_ATTN',
  },
};

describe('reading the settings of an entry', () => {
  it('shows each field as text and lists the rest as custom settings', () => {
    const draft = initialAdvanced(entry);

    expect(draft.values['min_context_fraction']).toBe('50');
    expect(draft.values['quantization']).toBe('awq');
    expect(draft.values['enforce_eager']).toBe('true');
    expect(draft.values['extra_args']).toBe('--a=1\n--b');
    expect(draft.values['max_num_seqs']).toBe('');
    expect(draft.custom).toEqual([
      { key: 'custom_flag', value: 'x' },
      { key: 'swap_space', value: 'TRITON_ATTN' },
    ]);
  });

  it('starts empty for a model without an entry and writes objects as JSON', () => {
    expect(initialAdvanced(undefined).custom).toEqual([]);
    const draft = initialAdvanced({ model_name: 'm', capability: {}, overrides: { chat_template_kwargs: { a: 1 } } });
    expect(draft.custom).toEqual([{ key: 'chat_template_kwargs', value: '{"a":1}' }]);
  });
});

describe('checking the settings', () => {
  const draft = (values: Record<string, string>, custom: { key: string; value: string }[] = []) => ({
    values: { ...initialAdvanced(undefined).values, ...values },
    custom,
  });

  it('accepts empty and valid values', () => {
    expect(advancedErrors(draft({}))).toEqual({});
    expect(
      advancedErrors(
        draft({ max_num_seqs: '8', gpu_memory_utilization: '0.9', min_context_fraction: '100', quantization: 'awq-marlin' }),
      ),
    ).toEqual({});
  });

  it('names what is wrong with each field', () => {
    const errors = advancedErrors(
      draft({
        max_num_seqs: '0',
        gpu_memory_utilization: '1.2',
        min_context_fraction: '150',
        quantization: 'a b',
        chat_template: '../x.jinja',
      }),
    );

    expect(errors['max_num_seqs']).toContain('whole number');
    expect(errors['gpu_memory_utilization']).toContain('0.1 to 1');
    expect(errors['min_context_fraction']).toContain('0 to 100');
    expect(errors['quantization']).toContain('letters');
    expect(errors['chat_template']).toContain('..');
    expect(advancedErrors(draft({ max_num_seqs: '2.5' }))['max_num_seqs']).toBeDefined();
  });

  it('refuses custom names that are malformed, that have a field of their own or that repeat', () => {
    const errors = advancedErrors(
      draft({}, [
        { key: 'Bad-Name', value: '1' },
        { key: 'quantization', value: 'awq' },
        { key: 'max_model_len', value: '1' },
        { key: 'ok_name', value: '1' },
        { key: 'ok_name', value: '2' },
        { key: '', value: 'ignored' },
      ]),
    );

    expect(Object.keys(errors).sort()).toEqual(['custom:0', 'custom:1', 'custom:2', 'custom:4']);
    expect(errors['custom:1']).toContain('own field');
    expect(errors['custom:4']).toContain('twice');
  });
});

describe('writing the settings', () => {
  it('keeps what is not edited and rebuilds the rest from the draft', () => {
    const draft = initialAdvanced(entry);
    draft.values['quantization'] = 'gptq';
    draft.values['max_num_seqs'] = '16';
    draft.values['enforce_eager'] = '';
    draft.values['min_context_fraction'] = '25';
    draft.values['extra_args'] = '--c=3\n\n  --d  ';
    draft.custom = [{ key: 'custom_flag', value: 'true' }, { key: 'depth', value: '3' }];

    const result = applyAdvanced(entry, draft);

    expect(result.capability).toEqual({
      tensor_parallel_size: 4,
      kv_cache_memory_bytes: '4G',
      min_context_fraction: 0.25,
      base_residency_mb: 900,
    });
    expect(result.overrides).toEqual({
      max_model_len: 32768,
      quantization: 'gptq',
      max_num_seqs: 16,
      extra_args: ['--c=3', '--d'],
      custom_flag: true,
      depth: 3,
    });
  });

  it('turns empty fields into no key at all and removed custom rows into removed keys', () => {
    const draft = initialAdvanced(entry);
    for (const key of Object.keys(draft.values)) draft.values[key] = '';
    draft.custom = [];

    const result = applyAdvanced(entry, draft);

    expect(result.overrides).toEqual({ max_model_len: 32768 });
    expect(result.capability).toEqual({ tensor_parallel_size: 4, kv_cache_memory_bytes: '4G', base_residency_mb: 900 });
  });

  it('writes on and off for the two-way fields and reads JSON values of custom rows', () => {
    const draft = initialAdvanced(undefined);
    draft.values['enable_prefix_caching'] = 'false';
    draft.values['enforce_eager'] = 'true';
    draft.values['gpu_memory_utilization'] = '0.85';
    draft.custom = [
      { key: 'chat_template_kwargs', value: '{"enable_thinking": false}' },
      { key: 'broken', value: '{oops' },
      { key: 'ratio', value: '1.5' },
    ];

    const result = applyAdvanced({ model_name: 'm', capability: {}, overrides: {} }, draft);

    expect(result.overrides).toEqual({
      enable_prefix_caching: false,
      enforce_eager: true,
      gpu_memory_utilization: 0.85,
      chat_template_kwargs: { enable_thinking: false },
      broken: '{oops',
      ratio: 1.5,
    });
  });
});

describe('listing the changes', () => {
  it('names changed fields and a changed custom list, and nothing else', () => {
    const start = initialAdvanced(entry);
    const now = structuredClone(start);
    expect(advancedChanges(start, now)).toEqual([]);

    now.values['quantization'] = 'gptq';
    now.values['extra_args'] = '';
    now.custom.push({ key: 'depth', value: '3' });

    expect(advancedChanges(start, now)).toEqual([
      'Quantization: awq → gptq',
      'Extra arguments: --a=1 --b → default',
      'Custom settings changed',
    ]);
  });
});

describe('the groups of the advanced settings', () => {
  it('puts every field in a group and keeps the keys apart', async () => {
    const { ADVANCED_FIELDS, FIELD_GROUPS } = await import('./node-model-advanced');
    const groups = new Set(FIELD_GROUPS.map((g) => g.id));

    expect(ADVANCED_FIELDS.every((f) => groups.has(f.group))).toBe(true);
    expect(new Set(ADVANCED_FIELDS.map((f) => f.key)).size).toBe(ADVANCED_FIELDS.length);
    expect(FIELD_GROUPS.filter((g) => g.top).map((g) => g.id)).toEqual(['placement', 'engine']);
  });

  it('offers a choice for the fields that have a fixed list of values', async () => {
    const { ADVANCED_FIELDS } = await import('./node-model-advanced');
    const select = (key: string) => ADVANCED_FIELDS.find((f) => f.key === key)!;

    expect(select('dtype').options!.map((o) => o.value)).toEqual(['', 'auto', 'half', 'bfloat16', 'float32']);
    expect(select('kv_cache_dtype').options!.map((o) => o.value)).toContain('fp8');
    expect(select('attention_backend').options!.map((o) => o.value)).toContain('FLASH_ATTN');
  });

  it('checks the new fields', () => {
    const draft = (values: Record<string, string>) => ({ values: { ...initialAdvanced(undefined).values, ...values }, custom: [] });

    expect(advancedErrors(draft({ cuda_graph_sizes: '1,2,4,8', gpu_devices: '0,2', cpu_offload_gb: '16', tool_call_parser: 'hermes' }))).toEqual({});
    const errors = advancedErrors(
      draft({ cuda_graph_sizes: '1, 2', gpu_devices: 'a', cpu_offload_gb: '-1', mm_processor_cache_gb: '2000', tool_call_parser: 'a b' }),
    );
    expect(Object.keys(errors).sort()).toEqual(['cpu_offload_gb', 'cuda_graph_sizes', 'gpu_devices', 'mm_processor_cache_gb', 'tool_call_parser']);
  });

  it('writes the GPU choice to capability and the others to the overrides', () => {
    const draft = initialAdvanced(undefined);
    draft.values['gpu_devices'] = '0,1';
    draft.values['dtype'] = 'bfloat16';
    draft.values['cpu_offload_gb'] = '8';
    draft.values['cuda_graph_sizes'] = '1,2';

    const result = applyAdvanced({ model_name: 'm', capability: {}, overrides: {} }, draft);

    expect(result.capability).toEqual({ gpu_devices: '0,1' });
    expect(result.overrides).toEqual({ dtype: 'bfloat16', cpu_offload_gb: 8, cuda_graph_sizes: '1,2' });
  });
});

describe('a setting that an imported file stored in capability', () => {
  it('is replaced, not copied, when the field is changed or reset', () => {
    const entry = { model_name: 'm', capability: { max_num_seqs: 8, dtype: 'half' }, overrides: {} };
    const draft = initialAdvanced(entry);
    draft.values['max_num_seqs'] = '16';
    draft.values['dtype'] = '';

    const result = applyAdvanced(entry, draft);

    expect(result.capability).toEqual({});
    expect(result.overrides).toEqual({ max_num_seqs: 16 });
  });
});
