import { NodeSettingsValues } from '../../../shared/models/node-config.model';

export type GpuMode = 'default' | 'all' | 'none' | 'pick';
export type Toggle = '' | 'true' | 'false';

/** What the editor holds: text per field, so empty means "not set, keep config.yml". */
export interface SettingsDraft {
  gpuMode: GpuMode;
  gpus: number[];
  args: string;
  reboot: Toggle;
  poll: string;
}

export const MAX_POLL_SECONDS = 300;
const MAX_ARGS = 200;

export function initialDraft(settings: NodeSettingsValues): SettingsDraft {
  const pool = settings.gpu_devices;
  const picked = pool !== undefined && pool !== 'all' && pool !== 'none';
  return {
    gpuMode: pool === undefined ? 'default' : picked ? 'pick' : (pool as GpuMode),
    gpus: picked ? pool.split(',').map(Number) : [],
    args: (settings.global_extra_args ?? []).join('\n'),
    reboot: settings.auto_reboot_on_stuck_gpu === undefined ? '' : (String(settings.auto_reboot_on_stuck_gpu) as Toggle),
    poll: settings.gpu_poll_interval === undefined ? '' : String(settings.gpu_poll_interval),
  };
}

const argList = (text: string): string[] =>
  text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');

const continuesAFlag = (args: string[], i: number): boolean =>
  i > 0 && args[i - 1].startsWith('-') && !args[i - 1].includes('=');

/** Problems that stop a save, by key. */
export function draftErrors(draft: SettingsDraft): Record<string, string> {
  const errors: Record<string, string> = {};
  if (draft.gpuMode === 'pick' && draft.gpus.length === 0) {
    errors['gpu_devices'] = 'Select at least one GPU, or choose another option.';
  }
  const args = argList(draft.args);
  if (args.length > MAX_ARGS) {
    errors['global_extra_args'] = `At most ${MAX_ARGS} arguments.`;
  } else if (args.some((arg, i) => !arg.startsWith('-') && !continuesAFlag(args, i))) {
    errors['global_extra_args'] = 'Every line starts with - (or is the value of the flag above it).';
  }
  const poll = draft.poll.trim();
  if (poll !== '' && !(/^\d+$/.test(poll) && Number(poll) >= 1 && Number(poll) <= MAX_POLL_SECONDS)) {
    errors['gpu_poll_interval'] = `Use whole seconds from 1 to ${MAX_POLL_SECONDS}.`;
  }
  return errors;
}

/** The document to store; a field left empty is not stored. */
export function toSettings(draft: SettingsDraft): NodeSettingsValues {
  const settings: NodeSettingsValues = {};
  const args = argList(draft.args);
  if (args.length > 0) {
    settings.global_extra_args = args;
  }
  if (draft.gpuMode === 'all' || draft.gpuMode === 'none') {
    settings.gpu_devices = draft.gpuMode;
  } else if (draft.gpuMode === 'pick') {
    settings.gpu_devices = [...draft.gpus].sort((a, b) => a - b).join(',');
  }
  if (draft.reboot !== '') {
    settings.auto_reboot_on_stuck_gpu = draft.reboot === 'true';
  }
  if (draft.poll.trim() !== '') {
    settings.gpu_poll_interval = Number(draft.poll);
  }
  return settings;
}

const LABELS: Record<keyof NodeSettingsValues, string> = {
  global_extra_args: 'Global vLLM arguments',
  gpu_devices: 'GPU pool',
  auto_reboot_on_stuck_gpu: 'Auto-reboot',
  gpu_poll_interval: 'GPU poll interval',
};

/** The settings that differ between two documents, for the save bar. */
export function settingsChanges(start: NodeSettingsValues, now: NodeSettingsValues): string[] {
  const shown = (value: unknown): string =>
    value === undefined ? 'default' : Array.isArray(value) ? `${value.length} argument(s)` : String(value);
  return (Object.keys(LABELS) as (keyof NodeSettingsValues)[])
    .filter((key) => JSON.stringify(start[key]) !== JSON.stringify(now[key]))
    .map((key) => `${LABELS[key]}: ${shown(start[key])} → ${shown(now[key])}`);
}

/** Changing the global flags reaches the plan of every model, so every calibration is measured again. */
export const remeasures = (start: NodeSettingsValues, now: NodeSettingsValues): boolean =>
  JSON.stringify(start.global_extra_args) !== JSON.stringify(now.global_extra_args);
