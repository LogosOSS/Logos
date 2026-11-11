import { Component, computed, input } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { ModelBenchmarkRun } from '../../shared/models/provider.model';

/** Historical jobs retain their stored status; only an explicit schedule cutoff is a normal stop. */
export function isBenchmarkWindowStop(run: ModelBenchmarkRun): boolean {
  return run.status === 'failed'
    && (run.result.failure?.details || run.error_message)?.trim() === 'window_closed_below_half';
}

@Component({
  selector: 'app-benchmark-run-failure', standalone: true, imports: [JsonPipe],
  template: `
    <section [attr.aria-label]="stopped() ? 'Benchmark stopped' : 'Benchmark failure'" [attr.role]="stopped() ? 'status' : 'alert'" [class.window-stop]="stopped()">
      @if (backend(); as name) { <strong>{{ name }} · {{ run().result.failure?.model || run().request?.model_name }}</strong> }
      @if (run().result.configuration_index; as index) {
        <p>Configuration {{ index }} · repetition {{ run().result.repetition }} · {{ run().result.completed_runs ?? 0 }}/{{ run().result.total_runs }} runs completed.</p>
      }
      <p>{{ summary() }}</p>
      <details><summary>{{ stopped() ? 'View stop details & configuration' : 'View error details & configuration' }}</summary>
        <pre>{{ run().result.failure?.details || run().error_message }}</pre>
        @if (run().result.failure; as failure) { <pre>{{ failure.settings | json }}</pre> }
      </details>
    </section>`,
  styles: [`:host{display:block;max-width:100%;min-width:0}section{border-left:3px solid rgb(var(--color-error));padding:8px 12px;overflow-wrap:anywhere}section.window-stop{border-color:rgb(var(--color-primary-500))}p{margin:6px 0}summary{cursor:pointer;color:rgb(var(--color-primary-500));padding:6px 0}pre{white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;font-size:12px;margin:8px 0}`],
})
export class BenchmarkRunFailure {
  readonly run = input.required<ModelBenchmarkRun>();
  readonly stopped = computed(() => isBenchmarkWindowStop(this.run()));
  readonly backend = computed(() => {
    if (this.run().result.failure) return this.run().result.failure!.attention_backend;
    const run = this.run();
    const index = run.result.configuration_index;
    const configuration = index ? run.request?.batch?.configurations[index - 1] : null;
    if (!configuration || typeof configuration !== 'object') return null;
    const overrides = (configuration as Record<string, unknown>)['serving_overrides'];
    if (!overrides || typeof overrides !== 'object') return null;
    const backend = (overrides as Record<string, unknown>)['attention_backend'];
    return typeof backend === 'string' ? backend : null;
  });
  readonly summary = computed(() => {
    if (this.stopped()) return 'The nightly window ended before this test was halfway complete. The schedule stopped the unfinished test; it is not used as a completed measurement. Completed measurements remain available in Benchmark runs.';
    const message = this.run().result.failure?.details || this.run().error_message || 'Benchmark failed.';
    const cause = message.match(/Cause: ([\s\S]*?)(?: Recent logs:|$)/)?.[1];
    if (cause) return cause;
    if (message.includes('Engine core initialization failed') || message.includes('vLLM exited during startup')) {
      return 'The model could not start. The recorded error does not identify the cause; inspect the worker startup logs. A return code alone does not prove backend incompatibility.';
    }
    return message.split('Recent logs:')[0].slice(0, 1200);
  });
}
