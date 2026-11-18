/** The stored model list of a logosnode and what Logos knows about each model. */

export interface NodeConfigEntry {
  model_name: string;
  capability: Record<string, unknown>;
  overrides: Record<string, unknown>;
}

export interface NodeConfig {
  provider_id: number;
  revision: number;
  yaml_imported: boolean;
  entries: NodeConfigEntry[];
}

export type CalibrationStatus = 'calibrated' | 'not_calibrated' | 'unsupported' | 'outdated';

export interface CalibrationPair {
  kv_mb: number | null;
  max_model_len: number | null;
  measured: boolean;
}

/** One calibrated tensor-parallel size of a model; today a model has one. */
export interface CalibrationResult {
  tp: number;
  active: boolean;
  base_residency_mb: number | null;
  loaded_vram_mb: number | null;
  kv_min_mb: number | null;
  kv_max_mb: number | null;
  pairs: CalibrationPair[];
  cold_load_time_s: number | null;
  wake_from_sleep_time_s: number | null;
  enforce_eager: boolean | null;
}

export interface CalibrationInsight {
  status: CalibrationStatus;
  reason: string | null;
  calibrated_at?: string | null;
  stale?: boolean;
  context_limit?: number | null;
  results: CalibrationResult[];
}

export interface BenchmarkResult {
  benchmark_id: number;
  throughput: number;
  settings: Record<string, unknown>;
  recorded_at: string | null;
  /** Mean time to first token and mean response time, in seconds, when the run measured them. */
  ttft_s?: number | null;
  latency_s?: number | null;
  /** Throughput of the run this one was compared with: what the model ran with before. */
  baseline_throughput?: number | null;
}

export interface BenchmarkInsight {
  best_eligible: BenchmarkResult | null;
  best_observed: BenchmarkResult | null;
  /** Serving settings of the lane that runs now. */
  current: Record<string, unknown>;
  eligible_for_comparison: boolean;
}

export interface NodeModelInsight {
  model_name: string;
  /** Id of the model in the Logos catalog, when it has one. */
  model_id?: number | null;
  /** The worker announces the model. */
  reported: boolean;
  /** A stored configuration row exists. */
  row: boolean;
  source: 'ui' | 'yaml_import' | 'tuning' | null;
  /** The row places the model: TP, KV cache and context are set. */
  configured: boolean;
  calibration: CalibrationInsight;
  benchmark: BenchmarkInsight | null;
  /** The worker calibrates (for the whole node, not this model alone). */
  calibrating: boolean;
}

export interface NodeGpuInsight {
  index: number;
  name: string | null;
  total_mb: number | null;
  used_mb: number | null;
}

export interface NodeInsights {
  provider_id: number;
  worker: {
    online: boolean;
    supports_actions: Record<string, boolean>;
    config_revision: number;
  };
  gpus: NodeGpuInsight[];
  models: NodeModelInsight[];
}

/** Node-wide settings an admin set; a key that is absent leaves the worker's config.yml value in force. */
export interface NodeSettingsValues {
  global_extra_args?: string[];
  gpu_devices?: string;
  auto_reboot_on_stuck_gpu?: boolean;
  gpu_poll_interval?: number;
}

export interface NodeSettings {
  provider_id: number;
  revision: number;
  yaml_imported: boolean;
  settings: NodeSettingsValues;
}

/** The answer to saving settings: whether the worker took them and which ones need a restart. */
export interface NodeSettingsSaved {
  provider_id: number;
  settings_revision: number;
  /** False when the worker is offline or too old; it takes the settings when it connects. */
  applied?: boolean;
  /** The worker keeps them until its running calibration ends. */
  deferred?: boolean;
  restart_needed?: string[];
}
