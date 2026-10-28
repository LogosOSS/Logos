import copy

import pytest

from logos.benchmarks import optimization as optimizer


def measured(identifier, throughput, *, sequences=16):
    return {
        "id": identifier,
        "sample_size": 1,
        "configuration": {
            "workload": {"id": "a" * 64},
            "nightly": {"series_key": "series", "parameters": {"max_num_seqs": sequences}},
        },
        "metrics": {
            "aggregate_output_tokens_per_second": throughput,
            "request_totals": {"total": 1, "successful": 1, "errored": 0, "incomplete": 0},
            "request_latency_ms": {"successful": {"percentiles": {"p95": 1000}}},
            "replay_samples": [{"source_log_id": 8, "output_tokens": 100, "finish_reason": "stop"}],
        },
    }


def test_search_reuses_winner_keeps_reference_and_resets_changed_conditions():
    current = {"max_num_seqs": 16, "max_num_batched_tokens": 4096, "dtype": "float16"}
    snapshot = {"runtime": {"devices": {"devices": [{"uuid": "gpu1", "used_memory_mb": 123}]}}}
    workload = {"id": "a" * 64, "name": "Trial", "request_count": 10, "provenance": "synthetic"}
    search = optimizer.context(current, snapshot, workload, {})
    search["state"].update(reference_id=1, best_parameters={"max_num_seqs": 32, "max_num_batched_tokens": 8192})
    next_search = optimizer.context(current, snapshot, workload, search["state"])
    plan = optimizer.plan(current, next_search)
    assert plan[0].serving_overrides.max_num_seqs == 16
    assert plan[1].serving_overrides.max_num_seqs == 32
    assert all(run.samples == 10 and run.workload_id == workload["id"] for run in plan)
    snapshot["runtime"]["devices"]["devices"][0]["used_memory_mb"] = 999
    assert optimizer.context(current, snapshot, workload, search["state"])["reference_id"] == 1
    snapshot["runtime"]["devices"]["devices"][0]["uuid"] = "gpu2"
    assert optimizer.context(current, snapshot, workload, search["state"])["reference_id"] is None
    assert (
        optimizer.context({**current, "dtype": "bfloat16"}, snapshot, workload, search["state"])["reference_id"] is None
    )


@pytest.mark.parametrize("failure", ["shorter", "length", "latency", "incomplete", "workload", "hardware"])
def test_faster_runs_fail_selection_when_controls_do_not_match(failure):
    baseline, candidate = measured(1, 40), measured(2, 80)
    sample = candidate["metrics"]["replay_samples"][0]
    if failure == "shorter":
        sample["output_tokens"] = 30
    if failure == "length":
        sample["finish_reason"] = "length"
    if failure == "latency":
        candidate["metrics"]["request_latency_ms"]["successful"]["percentiles"]["p95"] = 1200
    if failure == "incomplete":
        candidate["metrics"]["request_totals"]["incomplete"] = 1
    if failure == "workload":
        candidate["configuration"]["workload"]["id"] = "other"
    if failure == "hardware":
        candidate["configuration"]["nightly"]["series_key"] = "other"
    assert not optimizer.eligible(candidate, baseline)


def test_selection_retains_fixed_reference_and_measured_incumbent():
    search = {"state": {"iterations": 2}, "reference_id": 7}
    baseline, incumbent, slower = measured(10, 40), measured(11, 60, sequences=32), measured(12, 45, sequences=64)
    state = optimizer.select_state(
        search, [baseline, incumbent, slower], [{"configuration": 1, "status": "success", "benchmark_id": 10}]
    )
    assert state["reference_id"] == 7
    assert state["best_parameters"] == {"max_num_seqs": 32}
    assert state["iterations"] == 3
    previous = copy.deepcopy(state)
    assert optimizer.select_state({"state": state, "reference_id": 7}, [], []) == previous
