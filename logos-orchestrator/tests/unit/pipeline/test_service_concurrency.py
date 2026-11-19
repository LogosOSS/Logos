"""Scheduler queue waits use live service concurrency, separately from admission."""

from unittest.mock import MagicMock

import pytest

from logos import SchedulingRequest
from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.queue import PriorityQueueManager
from logos.queue.priority_queue import Priority
from logos.sdi.logosnode_facade import LogosNodeSchedulingDataFacade


def _lane(lane_id, parallel, *, active=0, running=0, state="loaded", model_name="model-a"):
    return {
        "lane_id": lane_id,
        "model": model_name,
        "runtime_state": state,
        "sleep_state": "sleeping" if state == "sleeping" else "awake",
        "num_parallel": parallel,
        "active_requests": active,
        "loaded_models": [{"name": model_name}] if state in ("loaded", "running") else [],
        "backend_metrics": {"queue_waiting": 0, "requests_running": running, "gpu_cache_usage_percent": 50},
    }


def _scheduler(monkeypatch, lanes_by_provider, configured=20, model_ids=None):
    class Registry:
        def peek_runtime_snapshot(self, provider_id):
            return {"runtime_revision": 1, "runtime": {"lanes": lanes_by_provider[provider_id]}}

        def is_provider_online(self, provider_id):
            return True

        def is_cordoned(self, provider_id):
            return False

    monkeypatch.setattr(
        "logos.sdi.providers.logosnode_provider.LogosNodeDataProvider._load_provider_config",
        lambda self: {"parallel_capacity": configured},
    )
    queue = PriorityQueueManager()
    facade = LogosNodeSchedulingDataFacade(queue, runtime_registry=Registry())
    for provider_id in lanes_by_provider:
        model_id = (model_ids or {}).get(provider_id, 1)
        model_name = lanes_by_provider[provider_id][0]["model"]
        facade.register_model(
            model_id, f"worker-{provider_id}", "http://fake", model_name, 65536, provider_id=provider_id
        )
    scheduler = ClassificationCorrectingScheduler(
        queue_manager=queue, logosnode_facade=facade, azure_facade=MagicMock()
    )
    return scheduler, facade, queue


@pytest.mark.parametrize("configured", [1, 20, 200, None])
async def test_congested_worker_does_not_win_on_its_loose_ledger_ceiling(monkeypatch, configured):
    scheduler, facade, queue = _scheduler(
        monkeypatch,
        {10: [_lane("busy", 1, active=1, running=1)], 20: [_lane("faster", 4, model_name="model-b")]},
        configured,
        model_ids={20: 2},
    )
    for index in range(2):
        queue.enqueue(f"queued-a-{index}", 1, priority=Priority.NORMAL)
    for index in range(3):
        queue.enqueue(f"queued-b-{index}", 2, priority=Priority.NORMAL)

    deployments = [
        {"model_id": model_id, "provider_id": provider_id, "type": "logosnode"}
        for model_id, provider_id in ((1, 10), (2, 20))
    ]
    request = SchedulingRequest(
        request_id="new-request", payload={}, classified_models=[(1, 1.0, 5), (2, 1.0, 5)], deployments=deployments
    )

    # Both workers can admit: ranking, rather than a capacity refusal, must
    # pick the faster peer. A 200-slot divisor would turn the real queue waits
    # into 0.03s and 0.045s, incorrectly preferring the slower worker 10.
    assert facade.get_parallel_capacity(1, 10) == (200, "runtime")
    assert facade.evaluate_admission(1, 10).can_admit is True
    assert facade.evaluate_admission(2, 20).can_admit is True
    assert scheduler._estimate_ettft(1, 10, "logosnode").queue_wait_s == pytest.approx(6.0)
    assert scheduler._estimate_ettft(2, 20, "logosnode").queue_wait_s == pytest.approx(2.25)

    result = await scheduler.schedule(request)

    assert result.provider_id == 20
    assert result.model_id == 2
    assert result.was_queued is False


def test_observed_short_request_concurrency_is_not_backlog(monkeypatch):
    scheduler, facade, _queue = _scheduler(monkeypatch, {10: [_lane("busy", 1, active=8, running=8)]})

    assert scheduler._estimate_ettft(1, 10, "logosnode").queue_wait_s == 0.0
    assert facade.evaluate_admission(1, 10).can_admit is True


def test_unabsorbed_active_requests_still_contribute_to_estimated_wait(monkeypatch):
    scheduler, _facade, _queue = _scheduler(monkeypatch, {10: [_lane("busy", 1, active=8, running=1)]})

    assert scheduler._estimate_ettft(1, 10, "logosnode").queue_wait_s == pytest.approx(21.0)


@pytest.mark.parametrize("running", [None, 0, float("nan"), float("inf"), float("-inf"), -1, "invalid"])
def test_unusable_running_metric_falls_back_to_reported_guarantee(monkeypatch, running):
    scheduler, _facade, queue = _scheduler(monkeypatch, {10: [_lane("lane", 4, running=running)]})
    queue.enqueue("queued", 1, priority=Priority.NORMAL)

    assert scheduler._estimate_ettft(1, 10, "logosnode").queue_wait_s == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("lanes", "expected_parallel"),
    [
        ([_lane("a", 2), _lane("b", 4, state="running")], 6),
        ([_lane("a", 2, active=5, running=5), _lane("b", 4, active=6, running=6)], 11),
        ([_lane("a", 2), _lane("sleeping", 100, state="sleeping"), _lane("starting", 100, state="starting")], 2),
        ([_lane("a", 2), _lane("stopped", 100, state="stopped"), _lane("failed", 100, state="error")], 2),
        ([_lane("unreported", None)], 1),
        ([_lane("unreported-guarantee", None, active=3, running=3)], 3),
        ([_lane("unreported-a", 0), _lane("unreported-b", 0)], 2),
        ([_lane("sleeping", 100, state="sleeping")], 1),
    ],
)
def test_queue_wait_counts_only_ready_service_lanes(monkeypatch, lanes, expected_parallel):
    scheduler, _facade, queue = _scheduler(monkeypatch, {10: lanes})
    for index in range(6):
        queue.enqueue(f"queued-{index}", 1, 10, Priority.NORMAL)

    estimate = scheduler._estimate_ettft(1, 10, "logosnode")

    assert estimate.queue_wait_s == pytest.approx(6 / expected_parallel * 3.0)
