# Benchmark-shaped E2E (GPU-less)

Approximates the dual-node × dual RTX 6000 Ada topology with the real
orchestrator and workernode. Only the layer below the worker is simulated
(`nvidia-smi` / fake vLLM / host-RAM telemetry). Results are synthetic —
**no GPU performance claims**.

## Limits (bounded default)

| Knob | Default | Full replay |
|------|---------|-------------|
| Requests / pattern | 10 | 1319 (`--full-replay`) |
| Patterns | burst, poisson, sequential, mixed | same |
| Models | 5 pinned HF ids | same |
| Request timeout | 180 s | 300 s |
| Idle → sleep L1 (configured) | 60 s | same |
| Lifecycle wake proof | capacity_pressure (same engine PID) | same |
| Per-card VRAM ceiling | 49140 MB | same |
| Simulated host RAM | 512 GiB total / 256 GiB available | same |

Full replay is a **runnable CLI**, not a config-dict assertion:

```bash
.venv/bin/python -m harness.workload --full-replay \
  --base-url http://localhost:18190 \
  --out /tmp/synthetic-full-replay.json
```

Optional arrival trace (CSV with `model_assigned`):

```bash
.venv/bin/python -m harness.workload --trace /path/to/arrivals.csv
```

## Fresh venv (exact logos_e2e.yml install — no PYTHONPATH)

```bash
cd e2e
rm -rf .venv-ci && uv venv .venv-ci
uv pip install --python .venv-ci/bin/python -e .
uv pip install --python .venv-ci/bin/python -r ../logos-workernode/requirements.txt
# Must pass with empty PYTHONPATH (CI does not inject orchestrator src):
env -u PYTHONPATH .venv-ci/bin/python -m pytest tests/gpu -v --timeout=120
```

## Stack lifecycle (external — tests never start/stop/restart)

Per `e2e/AGENTS.md`, pytest never owns compose up/down **or** orchestrator
restart. Use the **phased runner** (CI should invoke this, not a single
integrated pytest file):

```bash
cd e2e
export E2E_COMPOSE_PROJECT=logos-e2e-bench
export E2E_ORCHESTRATOR_PORT=18190
export E2E_DB_PORT=15432
export E2E_RESIDENCY_POLICY=demand   # or lru; default demand
export E2E_SIM_REPORT_PATH=/tmp/sim-report-demand/result.json
export E2E_SIM_SOURCE_SHA="$(git -C .. rev-parse HEAD)"
export E2E_BENCHMARK_TOPOLOGY=1

.venv/bin/python -m harness.stack.compose up --benchmark-topology
.venv/bin/python -m harness.stack.bootstrap_benchmark

# traffic → external --prepare-lifecycle (incl. orch restart) → lifecycle+report
.venv/bin/python -m harness.stack.run_phased_benchmark \
  --report "$E2E_SIM_REPORT_PATH" \
  --junit /tmp/sim-report-demand/junit.xml
echo EXIT=$?

# Gate (companion junit.xml next to result.json):
E2E_RESIDENCY_POLICY=demand E2E_SIM_SOURCE_SHA="$E2E_SIM_SOURCE_SHA" \
  python ../benchmarks/residency_simulation/pr_comment.py \
  --check --policy-file /tmp/sim-report-demand/result.json

# Sequential second policy (same project — recreate orch env, not parallel):
export E2E_RESIDENCY_POLICY=lru
export E2E_SIM_REPORT_PATH=/tmp/sim-report-lru/result.json
.venv/bin/python -m harness.stack.compose up --benchmark-topology
.venv/bin/python -m harness.stack.bootstrap_benchmark
.venv/bin/python -m harness.stack.run_phased_benchmark \
  --report "$E2E_SIM_REPORT_PATH" \
  --junit /tmp/sim-report-lru/junit.xml

.venv/bin/python -m harness.stack.compose down   # logos-e2e-bench only
```

### Phased runner contract

1. **Traffic pytest** (`tests/node/test_benchmark_traffic.py`) — topology + 5×4
   completions; on pass writes unique `E2E_SIM_PHASE_STATE` (real counts only).
2. **External prepare** — `prepare_lifecycle_isolation` with orchestrator
   restart + foreign `lanes/delete` (also
   `python -m harness.stack.bootstrap_benchmark --prepare-lifecycle`).
3. **Lifecycle pytest** (`tests/node/test_benchmark_lifecycle.py`) — requires
   `E2E_LIFECYCLE_PREPARED=1`; HTTP sleep/wake same-PID; emits schema v1 report.
4. **JUnit merge** — both phase XMLs merged; a failed traffic phase still
   produces junit (lifecycle may be absent → recorded as missing-artifact error).

Manual lifecycle-only pytest without the runner must set
`E2E_LIFECYCLE_PREPARED=1` after an external prepare; tests still must not
restart compose.

### Lifecycle pin (capacity_pressure)

Main 5×4 traffic uses both Ada nodes. External prepare then:

1. Restrict target + TP2 pressure models to `node-ada-a` via real
   `model_provider` rows.
2. Delete preexisting target/pressure engines on foreign providers via
   `POST /logosdb/providers/logosnode/lanes/delete` while sessions advertise lanes.
3. Restart the orchestrator (external / runner only); wait for workers.
4. Delete foreign engines again; fail closed if any remain.

Catalogue deletion alone does **not** unload a running/sleeping lane.
HTTP 200 on a probe is insufficient; the lifecycle test waits until the target
is **observed awake** on `node-ada-a`. Competing `/v1` requests force sleep;
wake asserts the **same engine PID**. Report `lifecycle_trigger` is
`capacity_pressure` (idle=60 remains configured; do not claim idle caused it).

Public profile defaults use **real** calibrated TP/KV/context (not uniform
4 GiB/8192). Timings are `accelerated_functional` only — never timing-faithful.
Frozen engine pins (`harness/fixtures/benchmark_engine_specs_public.json`, or
`E2E_ENGINE_SPECS_FIXTURE`) feed `engines.vllm.model_overrides` so Gemma-4b
calib KV ≈30 GiB vs engine pin ≈13 GiB exercises reconfigured cold-GMU
admission. Bootstrap seeds once; no mid-traffic DB profile rewrites. Host RAM
for sleep_L1 headroom is simulated below the worker OS collector
(`LOGOS_SIM_HOST_RAM_*`, 512 GiB total). Fake-engine physics lives under
`harness/gpusim/`.

## Synthetic performance comparison

The separate performance runner measures streaming TTFT, TTLT and time to every
outcome through the real orchestrator and workers. It resets its isolated stack
before each policy, pattern and seed. Only simulated GPU/vLLM interfaces use the
timing model. Workload hashes must agree across policies.

The local default is 250 requests per pattern, four patterns and three seeds
(42, 43, 44): 1,000 requests per policy and seed, 9,000 across all policies.
The CI probe uses 25 requests per pattern and seed 42 so it remains short.
CI still invokes `harness.stack.run_phased_benchmark` for functional lifecycle
coverage before the performance probe.

```bash
cd e2e
E2E_ORCHESTRATOR_PORT=18390 E2E_DB_PORT=15632 \
  .venv/bin/python -m harness.stack.run_performance_benchmark \
  --local-images --project logos-e2e-perf-local \
  --out /tmp/residency-performance/result.json
```

`--local-images` reuses images named `logos-e2e-bench-*` without building or
publishing an image. Source directories are mounted read-only. For other
prebuilt images, set `E2E_PERF_IMAGE_PROJECT` to their Compose project prefix.
Only project names beginning `logos-e2e-perf-` are accepted. The runner owns its
stack lifecycle and removes only that isolated project. It writes JSON with raw
requests and lifecycle events, a summary CSV and a Markdown table after every
finished case. A partial matrix remains explicitly incomplete.

`--policy`, `--pattern` and `--seed` can be repeated. `--rate` is the offered
arrival rate per simulated second; `--requests-per-pattern` controls sample size.
`--time-factor` defaults to 10. TTFT and TTLT quantiles include successful
streams only; time-to-outcome includes failures. The stream must contain content,
a finish reason and the terminal event. A failed stream never becomes a zero
latency sample. Percentiles use nearest rank. Lane switches count activation of
a different model on a GPU that previously served another model; initial
activation is not a switch. HTTP 507 wake rejections are counted at the fake
engine boundary. VRAM is checked from the shared ledger at lifecycle events.

The versioned `harness/gpusim/timing_profiles/dual_ada_v1.json` keeps calibration
cold-load and wake times, approximate cached-compile startup times, empirical
prompt/output lengths and inverse median TPOT. It includes sample counts and
input digests without source-machine identifiers or request text. Prefill is a
proxy estimated from the fastest TTFT decile. The concurrency slowdown curve
and eight engine slots are assumptions, not measured GPU capacities. Qwen
thinking is disabled in the workload template.

Engine service, arrivals, planner cycles, tenure, idle-sleep and failure backoff
scale with the factor. Worker status polling, process startup and HTTP overhead
remain wall time. Reported simulated seconds are wall seconds multiplied by the
factor, so factor sensitivity must be checked before interpreting small gaps.
These are synthetic relative-policy observations, not GPU performance or energy
measurements.
