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
