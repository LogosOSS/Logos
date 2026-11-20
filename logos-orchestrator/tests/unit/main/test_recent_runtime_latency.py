"""Adaptive latency learning follows new traffic and isolates engine replicas."""

from unittest.mock import MagicMock

import pytest

from logos.logosnode_registry import LogosNodeRuntimeRegistry
from logos.pipeline.latency_store import LatencyStore
from logos.runtime_latency import HistogramDelta


def _lane(lane_id, histogram, pid=100, **metrics):
    return {
        "model": "model",
        "lane_id": lane_id,
        "pid": pid,
        "backend_metrics": {"e2e_latency_histogram": histogram, **metrics},
    }


def test_recent_slow_requests_replace_short_warmup_history():
    store = LatencyStore()
    registry = LogosNodeRuntimeRegistry(latency_store=store)
    # The lifetime median stays at 1s throughout; the new workload takes 100s.
    for count in range(9):
        histogram = {"1": 1000, "100": 1000 + count, "+Inf": 1000 + count}
        registry._absorb_latency_observations(1, {"lanes": [_lane("a", histogram)]})
    assert store.get_e2e_latency_s("model", 1) == pytest.approx(100 - 99 * 0.8**8)


def test_replicas_do_not_reingest_each_others_counters():
    store = MagicMock()
    registry = LogosNodeRuntimeRegistry(latency_store=store)
    a = _lane("a", {"1": 10, "10": 10, "+Inf": 10})
    b = _lane("b", {"1": 0, "10": 20, "+Inf": 20})
    for _ in range(4):
        registry._absorb_latency_observations(1, {"lanes": [a, b]})
    assert [call.args[-1] for call in store.record_e2e_latency.call_args_list] == [1, 10]


def test_new_process_with_equal_count_is_not_deduplicated():
    store = MagicMock()
    registry = LogosNodeRuntimeRegistry(latency_store=store)
    registry._absorb_latency_observations(1, {"lanes": [_lane("a", {"1": 1, "10": 1, "+Inf": 1})]})
    registry._absorb_latency_observations(1, {"lanes": [_lane("a", {"1": 0, "10": 1, "+Inf": 1}, pid=101)]})
    assert [call.args[-1] for call in store.record_e2e_latency.call_args_list] == [1, 10]
    assert len(registry._lane_e2e_histograms) == 1


def test_disappeared_lane_state_is_retired_per_provider():
    registry = LogosNodeRuntimeRegistry(latency_store=MagicMock())
    lane = _lane("a", {"1": 1, "+Inf": 1})
    for provider in (1, 2):
        registry._absorb_latency_observations(provider, {"lanes": [lane]})
    registry._absorb_latency_observations(1, {"lanes": []})
    assert {key[0] for key in registry._lane_e2e_histograms} == {2}


def test_prefill_snapshot_is_learned_once_but_new_completions_are_learned():
    store = MagicMock()
    registry = LogosNodeRuntimeRegistry(latency_store=store)
    lane = _lane("a", {}, last_prefill_s=2.0, last_prefill_tokens=100, ttft_histogram={"+Inf": 1})
    for _ in range(3):
        registry._absorb_latency_observations(1, {"lanes": [lane]})
    assert store.record_prefill.call_count == 1
    lane["backend_metrics"]["ttft_histogram"]["+Inf"] = 2
    registry._absorb_latency_observations(1, {"lanes": [lane]})
    assert store.record_prefill.call_count == 2


def test_counter_reset_restarts_delta_baseline():
    delta = HistogramDelta()
    assert delta.observe({"1": 20, "10": 30, "+Inf": 30})
    assert delta.observe({"1": 0, "10": 1, "+Inf": 1}) == {"1": 0, "10": 1, "+Inf": 1}
    assert delta.observe({"1": 0, "10": 1, "+Inf": 1}) is None


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"1": 2, "+Inf": 1},
        {"1": -1, "+Inf": 1},
        {"1": float("nan"), "+Inf": 1},
        {"1": 1, "+Inf": float("inf")},
    ],
)
def test_invalid_scrape_keeps_good_baseline(bad):
    delta = HistogramDelta()
    delta.observe({"1": 1, "+Inf": 1})
    assert delta.observe(bad) is None
    assert delta.observe({"1": 2, "+Inf": 2}) == {"1": 1, "+Inf": 1}


def test_partial_bucket_reset_does_not_reingest_lifetime_counts():
    delta = HistogramDelta()
    delta.observe({"1": 10, "100": 20, "+Inf": 20})
    assert delta.observe({"1": 5, "100": 21, "+Inf": 21}) is None
    assert delta.observe({"1": 10, "100": 21, "+Inf": 21}) == {"1": 0, "100": 1, "+Inf": 1}
