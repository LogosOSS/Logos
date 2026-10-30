"""Persistent search context and conservative comparisons for one frozen workload."""

import copy
import hashlib
import math

from logos.benchmarks.configuration import ServingOverrides
from logos.benchmarks.nightly import parameter_plan
from logos.benchmarks.workloads import canonical

BATCHING_KEYS = {"max_num_seqs", "max_num_batched_tokens"}


def context(current, snapshot, workload, previous):
    """A new workload, serving environment or hardware starts a new fixed reference."""
    runtime = snapshot.get("runtime") or {}
    identity = {
        "workload": workload["id"],
        "measurement_engine": "guidellm",
        "serving": {key: value for key, value in current.items() if key not in BATCHING_KEYS | {"command"}},
        "devices": runtime.get("devices"),
        "worker_version": runtime.get("version_checksum") or runtime.get("service_version"),
        "profile": {"concurrency": 4, "transport": "streaming", "output_limits": "source_requests"},
    }
    # Device telemetry changes each heartbeat. Only identity/capacity belongs in a series.
    devices = identity["devices"]
    if isinstance(devices, (dict, list)):
        identity["devices"] = [
            {
                key: device.get(key)
                for key in ("uuid", "name", "kind", "index", "total_memory_mb", "memory_total_mb", "total_memory_bytes")
            }
            for device in (devices.get("devices", []) if isinstance(devices, dict) else devices)
        ]
    source_selection = workload.get("source_selection")
    if source_selection:
        identity["source"] = {key: source_selection[key] for key in ("username", "user_id")}
    environment_key = hashlib.sha256(
        canonical({key: value for key, value in identity.items() if key != "workload"}).encode()
    ).hexdigest()
    series_key = hashlib.sha256(canonical(identity).encode()).hexdigest()
    state = copy.deepcopy(previous) if previous.get("series_key") == series_key else {}
    if not state and source_selection and previous.get("environment_key") == environment_key:
        # A changed request cohort can reuse candidate parameters, never an older cohort's measured rate or reference.
        state["best_parameters"] = copy.deepcopy(previous.get("best_parameters") or {})
    state["environment_key"] = environment_key
    state["series_key"] = series_key
    state["workload_id"] = workload["id"]
    if source_selection:
        state["source"] = identity["source"]
    return {
        "state": state,
        "series_key": series_key,
        "reference_id": state.get("reference_id"),
        "workload_id": workload["id"],
        "source_selection": source_selection,
        "workload": {key: workload[key] for key in ("id", "name", "request_count", "provenance")},
    }


def round_index(state, day):
    session = state.get("nightly_session") or {}
    rounds = session.get("rounds", 0)
    return rounds if session.get("date") == str(day) and type(rounds) is int and rounds >= 0 else 0


def plan(current, search, *, round_index=0):
    """Measure production, the incumbent, then bounded neighbours of the incumbent."""
    baseline = parameter_plan(current)[0]
    best = search["state"].get("best_parameters") or {}
    center = {**current, **best}
    neighbours = parameter_plan(center)
    if round_index:
        radius = (0.25, 0.5, 0.125, 0.75, 0.375, 0.625, 0.875)[round_index % 7]
        explicit = {
            key: center[key] if isinstance(center.get(key), int) and center[key] > 0 else fallback
            for key, fallback in (("max_num_seqs", 4), ("max_num_batched_tokens", 2048))
        }
        trials = []
        for key in ("max_num_seqs", "max_num_batched_tokens"):
            for factor in (1 - radius, 1 + radius):
                candidate = {**explicit, key: max(1, int(explicit[key] * factor))}
                if candidate["max_num_seqs"] > 65536 or candidate["max_num_batched_tokens"] < candidate["max_num_seqs"]:
                    continue
                trials.append(baseline.model_copy(update={"serving_overrides": ServingOverrides(**candidate)}))
        neighbours = [neighbours[0], *trials]
    runs = [baseline]
    if best and best != baseline.serving_overrides.model_dump(exclude_none=True):
        runs.append(neighbours[0])
    runs.extend(neighbours[1:])
    seen = set()
    result = []
    for run in runs:
        key = canonical(run.serving_overrides.model_dump(exclude_none=True))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            run.model_copy(
                update={"workload_id": search["workload_id"], "samples": search["workload"]["request_count"]}
            )
        )
    return result


def rate(run):
    value = run["metrics"].get("aggregate_output_tokens_per_second")
    totals = run["metrics"].get("request_totals", {})
    count = run["sample_size"]
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value <= 0
        or count <= 0
        or totals != {"total": count, "successful": count, "errored": 0, "incomplete": 0}
    ):
        return None
    return value


def eligible(candidate, baseline):
    """Require the same tasks, complete answers, similar output lengths and bounded tail latency."""
    if rate(candidate) is None or rate(baseline) is None:
        return False
    candidate_config, base_config = candidate["configuration"], baseline["configuration"]
    if candidate_config.get("nightly", {}).get("series_key") != base_config.get("nightly", {}).get("series_key"):
        return False
    if candidate_config.get("workload", {}).get("id") != base_config.get("workload", {}).get("id"):
        return False
    base_samples = {row["source_log_id"]: row for row in baseline["metrics"].get("replay_samples", [])}
    samples = candidate["metrics"].get("replay_samples", [])
    if len(base_samples) != baseline["sample_size"] or len(samples) != len(base_samples):
        return False
    if {row["source_log_id"] for row in samples} != set(base_samples):
        return False
    for row in samples:
        original = base_samples[row["source_log_id"]]
        if row["finish_reason"] != original["finish_reason"] or row["finish_reason"] not in {"stop", "tool_calls"}:
            return False
        if row.get("native_finish_reason") != original.get("native_finish_reason"):
            return False
        tokens, before = row["output_tokens"], original["output_tokens"]
        if not (0.8 * before <= tokens <= 1.2 * before):
            return False

    def p95(run):
        return run["metrics"].get("request_latency_ms", {}).get("successful", {}).get("percentiles", {}).get("p95")

    before, after = p95(baseline), p95(candidate)
    return (
        type(before) in (int, float)
        and type(after) in (int, float)
        and math.isfinite(before)
        and math.isfinite(after)
        and before > 0
        and 0 < after <= before * 1.1
    )


def select_state(search, runs, report):
    """Persist an observed winner, not production adoption or a statistical claim."""
    state = copy.deepcopy(search["state"])
    by_id = {run["id"]: run for run in runs}
    baseline_id = next(
        (row.get("benchmark_id") for row in report if row.get("configuration") == 1 and row.get("status") == "success"),
        None,
    )
    baseline = by_id.get(baseline_id)
    if baseline is None or rate(baseline) is None:
        return state
    state["reference_id"] = search["reference_id"] or baseline_id
    valid = [baseline] + [run for run in runs if run["id"] != baseline_id and eligible(run, baseline)]
    winner = max(valid, key=lambda run: (rate(run), -run["id"]))
    previous_rate = state.get("best_throughput")
    if type(previous_rate) not in (int, float) or not math.isfinite(previous_rate) or rate(winner) > previous_rate:
        state["best_benchmark_id"] = winner["id"]
        state["best_parameters"] = winner["configuration"]["nightly"]["parameters"]
        state["best_throughput"] = rate(winner)
    state["iterations"] = state.get("iterations", 0) + 1
    state["guardrails"] = {"output_token_ratio": [0.8, 1.2], "max_p95_latency_ratio": 1.1}
    return state
