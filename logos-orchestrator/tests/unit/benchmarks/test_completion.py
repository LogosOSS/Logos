from logos.benchmarks.completion import description, result


def measurement(identifier, throughput):
    return {
        "id": identifier,
        "sample_size": 10,
        "configuration": {
            "workload": {"id": "frozen"},
            "nightly": {"baseline_id": 1, "original": {"max_num_seqs": 4}, "parameters": {"max_num_seqs": 5}},
        },
        "metrics": {
            "aggregate_output_tokens_per_second": throughput,
            "request_totals": {"total": 10, "successful": 10, "errored": 0, "incomplete": 0},
        },
    }


def test_completion_captures_a_concrete_current_result_and_its_changes():
    runs = [measurement(1, 27.1), measurement(2, 33.2), measurement(99, 100)]
    report = [{"benchmark_id": 1, "status": "success"}, {"benchmark_id": 2, "status": "success"}]
    captured = result(runs, report)
    assert captured["benchmark_id"] == 2
    assert captured["baseline_id"] == 1
    assert captured["changes"] == [{"parameter": "max_num_seqs", "before": 4, "after": 5}]
    assert "Benchmark #2" in description(captured)
    assert "max_num_seqs: 4 → 5" in description(captured)


def test_completion_links_to_the_adopted_result_even_with_a_faster_rejected_measurement():
    report = [{"benchmark_id": i, "status": "success"} for i in (1, 2)]
    report.append({"benchmark_id": 1, "status": "adopted"})
    assert result([measurement(1, 40), measurement(2, 80)], report)["benchmark_id"] == 1


def test_completion_without_successful_measurements_has_no_invented_target():
    assert result([measurement(1, 40)], [{"benchmark_id": 1, "status": "failed"}]) == {}
    assert description({}) == ""
