"""Persistent search context and conservative comparisons for one frozen workload."""

import copy
import hashlib
import math

from logos.benchmarks.configuration import ServingOverrides, canonical_kv_cache_dtype
from logos.benchmarks.workloads import canonical

BATCHING_KEYS = {"max_num_seqs", "max_num_batched_tokens"}
VARIED_KEYS = BATCHING_KEYS | {"kv_cache_dtype", "enable_prefix_caching", "speculative_config"}


def context(current, snapshot, workload, previous):
    """A new workload, serving environment or hardware starts a new fixed reference."""
    runtime = snapshot.get("runtime") or {}
    serving = {key: value for key, value in current.items() if key not in BATCHING_KEYS | {"command"}}
    cache_dtype = canonical_kv_cache_dtype(serving.get("kv_cache_dtype"))
    # Keep the worker's historical representation of the default in the series key.
    serving["kv_cache_dtype"] = "" if cache_dtype == "auto" else cache_dtype
    if not serving.get("speculative_config"):
        serving.pop("speculative_config", None)
    identity = {
        "workload": workload["id"],
        "measurement_engine": "guidellm",
        "serving": serving,
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


def comparison_diagnostics(candidate, baseline, *, include_length_changes=False):
    """Return adoption issues, the length mismatch count, and optional anonymous measurements."""
    issues = []
    lengths = 0
    length_changes = []
    if rate(candidate) is None or rate(baseline) is None:
        issues.append("A complete successful throughput measurement is missing")
    candidate_config, base_config = candidate["configuration"], baseline["configuration"]
    if candidate_config.get("nightly", {}).get("series_key") != base_config.get("nightly", {}).get("series_key"):
        issues.append("Measurement environments differ")
    workload_id = candidate_config.get("workload", {}).get("id")
    same_workload = workload_id == base_config.get("workload", {}).get("id")
    if not same_workload:
        issues.append("Request datasets differ")
    base_rows = baseline["metrics"].get("replay_samples") or []
    samples = candidate["metrics"].get("replay_samples") or []
    by_source = {row["source_log_id"]: row for row in samples}
    if (
        len(base_rows) != baseline["sample_size"]
        or len(by_source) != len(base_rows)
        or len(samples) != len(base_rows)
        or {row["source_log_id"] for row in base_rows} != set(by_source)
        or any(not _is_token_count(row.get("output_tokens")) for rows in (base_rows, samples) for row in rows)
    ):
        issues.append("Per-request comparison data is missing or the request sets differ")
    else:
        finishes, native = 0, 0
        for request_number, original in enumerate(base_rows, start=1):
            row = by_source[original["source_log_id"]]
            baseline_tokens, trial_tokens = original["output_tokens"], row["output_tokens"]
            reason = row.get("finish_reason")
            budget = row.get("fixed_output_budget")
            reached_fixed_budget = (
                reason == "length"
                and type(budget) is int
                and budget > 0
                and budget == original.get("fixed_output_budget") == trial_tokens == baseline_tokens
            )
            if reason != original.get("finish_reason") or (
                reason not in {"stop", "tool_calls"} and not reached_fixed_budget
            ):
                finishes += 1
            if row.get("native_finish_reason") != original.get("native_finish_reason"):
                native += 1
            if not (4 * baseline_tokens <= 5 * trial_tokens <= 6 * baseline_tokens):
                lengths += 1
                if include_length_changes and same_workload and workload_id:
                    length_changes.append(
                        {
                            "request_number": request_number,
                            "baseline_tokens": baseline_tokens,
                            "trial_tokens": trial_tokens,
                            "change_percent": (
                                100 * (trial_tokens - baseline_tokens) / baseline_tokens if baseline_tokens else None
                            ),
                        }
                    )
        if finishes:
            issues.append(f"{finishes} answers have a different or incomplete finish reason")
        if native:
            issues.append(f"{native} answers have a different native finish reason")
        if lengths:
            issues.append(f"{lengths} answer lengths differ by more than 20% from baseline")

    before, after = p95_latency(baseline), p95_latency(candidate)
    if not (
        type(before) in (int, float)
        and type(after) in (int, float)
        and math.isfinite(before)
        and math.isfinite(after)
        and before > 0
        and after > 0
    ):
        issues.append("Comparable p95 request latency is missing")
    elif after > before * 1.1:
        issues.append(f"p95 latency increased by {(after / before - 1) * 100:.1f}% (maximum 10%)")
    return issues, lengths if same_workload and workload_id else 0, length_changes


def p95_latency(run):
    """Return the successful-request p95 used to compare serving configurations."""
    value = run["metrics"].get("request_latency_ms", {}).get("successful", {}).get("percentiles", {}).get("p95")
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def trial_eligible(candidate, baseline, *, frozen_workload):
    """Compare a completed trial to a baseline, including replay checks where available."""
    if frozen_workload:
        return eligible(candidate, baseline)
    before, after = p95_latency(baseline), p95_latency(candidate)
    return rate(candidate) is not None and before is not None and after is not None and after <= before * 1.1


def _is_token_count(value):
    return (type(value) is int or (type(value) is float and value.is_integer())) and value >= 0


def eligibility_issues(candidate, baseline):
    """Explain the response, workload and latency checks used for automatic adoption."""
    issues, _, _ = comparison_diagnostics(candidate, baseline)
    return issues


def eligible(candidate, baseline):
    """Require the same tasks, complete answers, similar output lengths and bounded tail latency."""
    return not eligibility_issues(candidate, baseline)


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
    if "adaptive_baseline" in search:
        state["adaptive_baseline"] = search["adaptive_baseline"]
        attempted = {canonical(params): params for params in search["attempted"]}
        for outcome in report:
            if outcome.get("kind") == "trial" and outcome.get("status") in {"success", "failed", "stalled"}:
                params = outcome["parameters"]
                attempted[canonical(params)] = params
        state["attempted_parameters"] = list(attempted.values())[-128:]
    state["iterations"] = state.get("iterations", 0) + 1
    state["guardrails"] = {"output_token_ratio": [0.8, 1.2], "max_p95_latency_ratio": 1.1}
    return state


def adoption_candidate(search, runs, report):
    """Select only a measured improvement over this round's production baseline."""
    baseline_id = next(
        (row.get("benchmark_id") for row in report if row.get("configuration") == 1 and row.get("status") == "success"),
        None,
    )
    baseline = next((run for run in runs if run["id"] == baseline_id), None)
    if baseline is None or rate(baseline) is None:
        return None
    config = baseline["configuration"]
    if (
        config.get("nightly", {}).get("series_key") != search["series_key"]
        or config.get("workload", {}).get("id") != search["workload_id"]
    ):
        return None
    by_id = {run["id"]: run for run in runs}
    trials = {
        row["benchmark_id"]
        for row in report
        if row.get("kind") == "trial" and row.get("status") == "success" and row["benchmark_id"] != baseline_id
    }

    def qualifies(run):
        return run is not None and eligible(run, baseline) and rate(run) > rate(baseline)

    candidates = []
    for row in report:
        if row.get("kind") != "confirmation" or row.get("status") != "success":
            continue
        original_id, confirmation_id = row.get("confirms_benchmark_id"), row.get("benchmark_id")
        if original_id not in trials or confirmation_id in trials or confirmation_id == baseline_id:
            continue
        trial, confirmation = by_id.get(original_id), by_id.get(confirmation_id)
        if not qualifies(trial) or not qualifies(confirmation):
            continue
        parameters = confirmation["configuration"]["nightly"].get("parameters") or {}
        if parameters != trial["configuration"]["nightly"].get("parameters"):
            continue
        if not parameters or parameters.keys() - VARIED_KEYS:
            continue
        if any(type(parameters[key]) is not int or parameters[key] <= 0 for key in BATCHING_KEYS & parameters.keys()):
            continue
        if parameters.get("kv_cache_dtype", "auto") not in {"auto", "fp8"}:
            continue
        if type(parameters.get("enable_prefix_caching", False)) is not bool:
            continue
        try:
            overrides = ServingOverrides(**parameters)
        except ValueError:
            continue
        candidates.append((min(rate(trial), rate(confirmation)), confirmation["id"], original_id, overrides))
    if not candidates:
        return None
    _, confirmation_id, trial_id, overrides = max(candidates, key=lambda item: (item[0], -item[1]))
    return {
        "benchmark_id": confirmation_id,
        "confirmation_benchmark_id": confirmation_id,
        "trial_benchmark_id": trial_id,
        "baseline_id": baseline_id,
        "workload_id": search["workload_id"],
        "parameters": overrides.model_dump(exclude_none=True),
        "throughput": rate(by_id[confirmation_id]),
        "baseline_throughput": rate(baseline),
    }
