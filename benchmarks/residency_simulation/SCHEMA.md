# GPU-less residency simulation — report schema (v1)

The E2E suite writes one JSON object to the path in `E2E_SIM_REPORT_PATH`
after each `E2E_RESIDENCY_POLICY` run. The CI reporter gates and comments on
those facts only — missing or invented data is **FAIL** / `NOTAVAILABLE`.

Compose must pass the policy through (default `demand`):

```yaml
LOGOS_RESIDENCY_POLICY: ${E2E_RESIDENCY_POLICY:-demand}
```

## Environment

| Variable | Role |
|---|---|
| `E2E_RESIDENCY_POLICY` | `demand` or `lru` (matrix leg) |
| `E2E_SIM_REPORT_PATH` | Absolute path of the JSON file to write |
| `E2E_SIM_SOURCE_SHA` | Expected 40-hex PR-head SHA. Emitter must `git rev-parse HEAD` and verify it matches this value — do not trust the merge-ref `GITHUB_SHA` blindly. |

## JSON object

```json
{
  "schema_version": 1,
  "source_sha": "<exactly 40 hex chars; must equal git rev-parse HEAD / E2E_SIM_SOURCE_SHA>",
  "residency_policy": "demand" | "lru",
  "synthetic": true,
  "scientific_claims": false,
  "patterns": [
    {
      "pattern": "burst" | "poisson" | "sequential" | "mixed",
      "submitted": 5,
      "completed": 5,
      "failures": 0,
      "missing_models": []
    }
  ],
  "per_model_failures": { "<model id>": 0 },
  "lifecycle": { "cold_load": 1, "sleep": 1, "wake": 1 },
  "vram": { "observation_count": 1, "violations": 0 },
  "functional_verdict": "PASS" | "FAIL"
}
```

### PASS requirements

- `schema_version == 1`, `synthetic == true`, `scientific_claims == false`
- `source_sha` exactly 40 hex; agrees with `E2E_SIM_SOURCE_SHA` when set; agrees across matrix legs
- `residency_policy` equals the matrix leg
- Exactly the four distinct patterns; each: integer counters (not bool), `submitted >= 5`, `completed == submitted`, `failures == 0`, `missing_models == []`
- `lifecycle.cold_load` / `sleep` / `wake` each integer `>= 1` (must prove exercised)
- `vram.observation_count >= 1`, `vram.violations == 0`
- `functional_verdict == "PASS"`
- Companion `junit.xml` (integrated pytest only) required: `tests > 0`, `failures == 0`, `errors == 0`. `junit-gpu.xml` is not a substitute.

### Comment cells

Missing reports render as `NOTAVAILABLE` (never as invented `0`). Startup or artifact failure still posts a red comment.

## Local commands

```bash
python -m pytest benchmarks/residency_simulation/tests -q
E2E_SIM_SOURCE_SHA=$(git rev-parse HEAD) \
LOGOS_SIM_REPORT_DIR=/tmp/sim-reports \
LOGOS_BENCH_PR_COMMENT_DRY_RUN=1 \
  python benchmarks/residency_simulation/pr_comment.py
```
