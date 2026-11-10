# GPU-less residency simulation (CI reporter)

Functional exercise of residency policies (`demand` / `lru`) against the real
orchestrator and workernode with a fake vLLM below the worker. **Not a GPU
performance benchmark** — timings are synthetic; results must not make
scientific claims.

The E2E suite brings up the dual-Ada benchmark topology, runs the integrated
tests, and writes observations to `E2E_SIM_REPORT_PATH`. This directory
**gates and comments** on those facts.

## Layout

| Path | Role |
|---|---|
| `SCHEMA.md` | JSON field contract for emitters and the gate |
| `report.py` | Schema validation, fail-closed gate, markdown tables |
| `pr_comment.py` | Aggregate matrix legs; markdown / optional API post |
| `tests/` | Parse, startup failure, partial data, invalid schema, mocked API, workflow contract |

Idempotent PR comment anchor: `<!-- logos-residency-simulation-bench -->`.

## Local checks (no Docker)

```bash
python -m pytest benchmarks/residency_simulation/tests -q
E2E_SIM_SOURCE_SHA=$(git rev-parse HEAD) \
LOGOS_SIM_REPORT_DIR=/tmp/sim-reports \
LOGOS_BENCH_PR_COMMENT_DRY_RUN=1 \
  python benchmarks/residency_simulation/pr_comment.py
```

## CI

Workflow: `.github/workflows/logos_benchmark-simulation.yml`

- Checks out `pull_request.head.sha` (not the merge ref); sets `E2E_SIM_SOURCE_SHA` to that head.
- Matrix `E2E_RESIDENCY_POLICY=demand|lru` with isolated compose projects and ports.
- `simulate` (read-only): compose `--benchmark-topology`, bootstrap, pytest + JUnit, upload per-policy artifacts; always tear down its project.
- `aggregate` (read-only): download artifacts (tolerant), render `comment.md`, upload it, then fail the gate if red.
- `post-comment` (write token): downloads `comment.md` only and posts via inline `actions/github-script@v9` — no PR checkout or PR Python. Forks soft-fail the post step; the gate stays red.
- Missing integrated `junit.xml` fails even when `junit-gpu.xml` is present.
- Missing artifacts still render and post a red comment (`NOTAVAILABLE` cells).
