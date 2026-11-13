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


@pytest.mark.parametrize(
    "failure", ["shorter", "length", "native_reason", "latency", "incomplete", "workload", "hardware"]
)
def test_faster_runs_fail_selection_when_controls_do_not_match(failure):
    baseline, candidate = measured(1, 40), measured(2, 80)
    sample = candidate["metrics"]["replay_samples"][0]
    if failure == "shorter":
        sample["output_tokens"] = 30
    if failure == "length":
        sample["finish_reason"] = "length"
    if failure == "native_reason":
        baseline["metrics"]["replay_samples"][0]["native_finish_reason"] = "end_turn"
        sample["native_finish_reason"] = "stop_sequence"
    if failure == "latency":
        candidate["metrics"]["request_latency_ms"]["successful"]["percentiles"]["p95"] = 1200
    if failure == "incomplete":
        candidate["metrics"]["request_totals"]["incomplete"] = 1
    if failure == "workload":
        candidate["configuration"]["workload"]["id"] = "other"
    if failure == "hardware":
        candidate["configuration"]["nightly"]["series_key"] = "other"
    assert not optimizer.eligible(candidate, baseline)


@pytest.mark.parametrize("candidate_budget", [None, 90, 100])
def test_only_matching_fixed_output_budgets_are_eligible(candidate_budget):
    baseline, candidate = measured(1, 40), measured(2, 80)
    baseline["metrics"]["replay_samples"][0].update(finish_reason="length", fixed_output_budget=100)
    candidate["metrics"]["replay_samples"][0].update(finish_reason="length", fixed_output_budget=candidate_budget)
    assert optimizer.eligible(candidate, baseline) is (candidate_budget == 100)


def test_selection_retains_fixed_reference_and_measured_incumbent():
    search = {"state": {"iterations": 2}, "reference_id": 7}
    baseline, incumbent, slower = measured(10, 40), measured(11, 60, sequences=32), measured(12, 45, sequences=64)
    state = optimizer.select_state(
        search, [baseline, incumbent, slower], [{"configuration": 1, "status": "success", "benchmark_id": 10}]
    )
    assert state["reference_id"] == 7
    assert state["best_parameters"] == {"max_num_seqs": 32}
    assert state["iterations"] == 3
    lower = optimizer.select_state(
        {"state": state, "reference_id": 7},
        [measured(20, 30), measured(21, 40, sequences=64)],
        [{"configuration": 1, "status": "success", "benchmark_id": 20}],
    )
    assert lower["best_benchmark_id"] == 11
    assert lower["best_parameters"] == {"max_num_seqs": 32}
    assert lower["best_throughput"] == 60
    assert lower["iterations"] == 4
    previous = copy.deepcopy(state)
    assert optimizer.select_state({"state": state, "reference_id": 7}, [], []) == previous


def test_changed_daily_cohort_retains_candidate_parameters_without_comparing_old_rates():
    current = {"max_num_seqs": 16, "max_num_batched_tokens": 4096}
    workload = {
        "id": "a" * 64,
        "name": "Recent requests",
        "request_count": 50,
        "provenance": "production",
        "source_selection": {"username": "source.user", "user_id": 1},
    }
    first = optimizer.context(current, {}, workload, {})
    previous = {**first["state"], "best_parameters": {"max_num_seqs": 32}, "best_throughput": 99, "reference_id": 7}
    next_night = optimizer.context(current, {}, {**workload, "id": "b" * 64}, previous)
    assert next_night["state"]["best_parameters"] == {"max_num_seqs": 32}
    assert "best_throughput" not in next_night["state"]
    assert next_night["reference_id"] is None
    assert optimizer.context(current, {}, workload, previous)["state"]["best_throughput"] == 99
    different_user = {**workload, "source_selection": {"username": "another.user", "user_id": 2}}
    assert "best_parameters" not in optimizer.context(current, {}, different_user, previous)["state"]


def test_further_rounds_explore_new_radii_on_the_same_frozen_requests():
    current = {"max_num_seqs": 16, "max_num_batched_tokens": 4096}
    search = {"state": {}, "workload_id": "a" * 64, "workload": {"request_count": 50}}
    plans = [optimizer.plan(current, search, round_index=index) for index in range(3)]
    values = [
        {tuple(sorted(run.serving_overrides.model_dump(exclude_none=True).items())) for run in plan[1:]}
        for plan in plans
    ]
    assert len(set.union(*values)) == 12
    for plan in plans:
        assert plan[0].serving_overrides.model_dump(exclude_none=True) == current
        assert all(run.samples == 50 and run.workload_id == "a" * 64 for run in plan)
        assert all(
            sum(value != current[key] for key, value in run.serving_overrides.model_dump(exclude_none=True).items())
            == 1
            for run in plan[1:]
        )
    assert optimizer.round_index({"nightly_session": {"date": "2026-10-29", "rounds": 2}}, "2026-10-29") == 2
    assert optimizer.round_index({"nightly_session": {"date": "2026-10-28", "rounds": 7}}, "2026-10-29") == 0


@pytest.mark.parametrize("throughput", [39, 40, 41])
def test_adoption_requires_a_fresh_strict_improvement_not_a_historical_record(throughput):
    baseline, candidate = measured(10, 40), measured(11, throughput, sequences=32)
    search = {"series_key": "series", "workload_id": "a" * 64, "state": {"best_throughput": 100}}
    report = [
        {"configuration": 1, "status": "success", "benchmark_id": 10},
        {"configuration": 2, "status": "success", "benchmark_id": 11},
    ]
    adoption = optimizer.adoption_candidate(search, [baseline, candidate], report)
    if throughput <= 40:
        assert adoption is None
    else:
        assert adoption["benchmark_id"] == 11
        assert adoption["baseline_id"] == 10
        assert adoption["parameters"] == {"max_num_seqs": 32}


@pytest.mark.parametrize("failure", ["failed", "shorter", "baseline", "series", "dataset", "unrelated_parameter"])
def test_adoption_rejects_unverified_or_unrelated_measurements(failure):
    baseline, candidate = measured(10, 40), measured(11, 80, sequences=32)
    search = {"series_key": "series", "workload_id": "a" * 64}
    report = [
        {"configuration": 1, "status": "success", "benchmark_id": 10},
        {"configuration": 2, "status": "success", "benchmark_id": 11},
    ]
    if failure == "failed":
        report[1]["status"] = "failed"
    if failure == "baseline":
        report[0]["status"] = "failed"
    if failure == "shorter":
        candidate["metrics"]["replay_samples"][0]["output_tokens"] = 50
    if failure == "series":
        search["series_key"] = "changed"
    if failure == "dataset":
        search["workload_id"] = "changed"
    if failure == "unrelated_parameter":
        candidate["configuration"]["nightly"]["parameters"]["dtype"] = "auto"
    assert optimizer.adoption_candidate(search, [baseline, candidate], report) is None


def test_selection_explains_multiple_failures_without_disclosing_request_content():
    baseline, candidate = measured(1, 40), measured(2, 80)
    candidate["metrics"]["replay_samples"][0]["output_tokens"] = 30
    candidate["metrics"]["request_latency_ms"]["successful"]["percentiles"]["p95"] = 1200
    assert optimizer.eligibility_issues(candidate, baseline) == [
        "1 answer lengths differ by more than 20% from baseline",
        "p95 latency increased by 20.0% (maximum 10%)",
    ]
    assert not optimizer.eligible(candidate, baseline)
