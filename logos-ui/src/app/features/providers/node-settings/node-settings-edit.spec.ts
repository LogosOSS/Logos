import { describe, expect, it } from 'vitest';

import { draftErrors, initialDraft, remeasures, settingsChanges, toSettings } from './node-settings-edit';

describe('reading the settings of a node', () => {
  it('shows each stored value and leaves the rest on default', () => {
    const draft = initialDraft({
      global_extra_args: ['--a=1', '--b'],
      gpu_devices: '0,2',
      auto_reboot_on_stuck_gpu: false,
      gpu_poll_interval: 7,
    });

    expect(draft).toEqual({ gpuMode: 'pick', gpus: [0, 2], args: '--a=1\n--b', reboot: 'false', poll: '7' });
    expect(initialDraft({})).toEqual({ gpuMode: 'default', gpus: [], args: '', reboot: '', poll: '' });
    expect(initialDraft({ gpu_devices: 'all' }).gpuMode).toBe('all');
    expect(initialDraft({ gpu_devices: 'none' }).gpuMode).toBe('none');
  });
});

describe('writing the settings of a node', () => {
  it('stores only what is set', () => {
    expect(toSettings(initialDraft({}))).toEqual({});
    expect(
      toSettings({ gpuMode: 'pick', gpus: [2, 0], args: ' --a \n\n--b ', reboot: 'true', poll: '9' }),
    ).toEqual({ global_extra_args: ['--a', '--b'], gpu_devices: '0,2', auto_reboot_on_stuck_gpu: true, gpu_poll_interval: 9 });
    expect(toSettings({ gpuMode: 'all', gpus: [], args: '', reboot: '', poll: '' })).toEqual({ gpu_devices: 'all' });
    expect(toSettings({ gpuMode: 'none', gpus: [], args: '', reboot: 'false', poll: '' })).toEqual({
      gpu_devices: 'none',
      auto_reboot_on_stuck_gpu: false,
    });
  });

  it('round-trips what it reads', () => {
    const settings = { global_extra_args: ['--a', 'x'], gpu_devices: '1', auto_reboot_on_stuck_gpu: true, gpu_poll_interval: 3 };

    expect(toSettings(initialDraft(settings))).toEqual(settings);
  });
});

describe('checking the settings of a node', () => {
  const draft = (over: object = {}) => ({ ...initialDraft({}), ...over });

  it('accepts an untouched draft and valid values', () => {
    expect(draftErrors(draft())).toEqual({});
    expect(draftErrors(draft({ args: '--a\n--b value\n--c=1', poll: '300' }))).toEqual({});
    expect(draftErrors(draft({ args: '--max-num-seqs\n8' }))).toEqual({});
  });

  it('names what is wrong with each field', () => {
    const errors = draftErrors(draft({ gpuMode: 'pick', args: 'stray', poll: '0' }));

    expect(Object.keys(errors).sort()).toEqual(['global_extra_args', 'gpu_devices', 'gpu_poll_interval']);
    expect(draftErrors(draft({ args: '--a=1\nvalue' }))['global_extra_args']).toBeDefined();
    expect(draftErrors(draft({ poll: '2.5' }))['gpu_poll_interval']).toBeDefined();
    expect(draftErrors(draft({ poll: '301' }))['gpu_poll_interval']).toBeDefined();
    expect(draftErrors(draft({ args: Array(201).fill('--a').join('\n') }))['global_extra_args']).toContain('200');
  });
});

describe('listing the changes', () => {
  it('names what differs and nothing else', () => {
    expect(settingsChanges({}, {})).toEqual([]);
    expect(
      settingsChanges(
        { gpu_devices: 'all', gpu_poll_interval: 5 },
        { gpu_devices: '0', global_extra_args: ['--a'], gpu_poll_interval: 5, auto_reboot_on_stuck_gpu: false },
      ),
    ).toEqual([
      'Global vLLM arguments: default → 1 argument(s)',
      'GPU pool: all → 0',
      'Auto-reboot: default → false',
    ]);
  });

  it('warns about re-measuring only when the global arguments change', () => {
    expect(remeasures({}, { gpu_devices: '0' })).toBe(false);
    expect(remeasures({ global_extra_args: ['--a'] }, { global_extra_args: ['--a'] })).toBe(false);
    expect(remeasures({}, { global_extra_args: ['--a'] })).toBe(true);
    expect(remeasures({ global_extra_args: ['--a'] }, {})).toBe(true);
  });
});
