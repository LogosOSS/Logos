from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from logos.benchmarks import nightly_store


def measurement(identifier, workload, throughput, baseline_id=None):
    return {
        "id": identifier,
        "sample_size": 1,
        "configuration": {
            "workload": {"id": workload},
            "nightly": {"baseline_id": baseline_id, "series_key": "same-worker"},
        },
        "metrics": {
            "aggregate_output_tokens_per_second": throughput,
            "request_totals": {"total": 1, "successful": 1, "errored": 0, "incomplete": 0},
            "replay_samples": [{"source_log_id": 10, "output_tokens": 100, "finish_reason": "stop"}],
            "request_latency_ms": {"successful": {"percentiles": {"p95": 1000}}},
        },
    }


@pytest.mark.parametrize("workload_id", [None, "current"])
def test_selection_summary_distinguishes_an_unverified_record_from_an_eligible_result(monkeypatch, workload_id):
    db = Mock()
    db.session.execute.return_value.mappings.return_value = [
        measurement(15, "current", None, baseline_id=10),
        measurement(14, "current", 55),
        measurement(13, "other", 100, baseline_id=13),
        measurement(11, "current", 47, baseline_id=10),
        measurement(10, "current", 40, baseline_id=10),
    ]
    monkeypatch.setattr(nightly_store, "DBManager", lambda: nullcontext(db))

    summary = nightly_store.selection_summary(31, workload_id)

    assert summary["best_observed"]["benchmark_id"] == 14
    assert summary["best_observed"]["issues"] == ["No comparison baseline was recorded"]
    assert summary["best_eligible"]["benchmark_id"] == 11
    assert [item["benchmark_id"] for item in summary["comparisons"]] == [15, 14, 11, 10]
    assert summary["comparisons"][0]["throughput"] is None


def test_selection_summary_has_no_winner_when_no_measurement_is_available(monkeypatch):
    db = Mock()
    db.session.execute.return_value.mappings.return_value = []
    monkeypatch.setattr(nightly_store, "DBManager", lambda: nullcontext(db))

    assert nightly_store.selection_summary(31) == {
        "best_observed": None,
        "best_eligible": None,
        "comparisons": [],
    }
