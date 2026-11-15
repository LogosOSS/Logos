"""Candidate observations describe the decision without changing reservation order."""

import json
from unittest.mock import AsyncMock

import pytest

from logos.pipeline.ettft_estimator import EttftEstimate, ReadinessTier
from logos.pipeline.scheduler_interface import SchedulingResult
from tests.unit.pipeline.test_correcting_scheduler import _make_request, _make_scheduler


def _candidate(provider_id, tier, wait, provider_type="logosnode"):
    return (1, provider_id, provider_type, -wait, 5, EttftEstimate(wait, tier, "observed"))


def test_snapshot_preserves_order_shared_queue_and_unavailable_values(monkeypatch):
    scheduler = _make_scheduler()
    monkeypatch.setattr(scheduler._queue_mgr, "get_total_depth_by_model", lambda model_id: 3)
    scored = [
        _candidate(20, ReadinessTier.WARM, 0.2),
        _candidate(10, ReadinessTier.COLD, 45),
        _candidate(30, ReadinessTier.UNAVAILABLE, float("inf"), "cloud"),
    ]
    snapshot = scheduler._snapshot_candidates(scored)
    assert [row["provider_id"] for row in snapshot] == [20, 10, 30]
    assert [row["queue_depth"] for row in snapshot] == [3, 3, None]
    assert snapshot[2]["expected_wait_s"] is None
    assert snapshot[0]["state"] == "warm"
    json.dumps(snapshot, allow_nan=False)


@pytest.mark.asyncio
async def test_queue_snapshot_precedes_wait_and_survives_dispatch_to_another_node(monkeypatch):
    scheduler = _make_scheduler()
    scored = [_candidate(10, ReadinessTier.COLD, 45), _candidate(20, ReadinessTier.COLD, 50)]
    monkeypatch.setattr(scheduler, "_compute_candidate_scores", lambda *args, **kwargs: scored)
    monkeypatch.setattr(scheduler, "_try_immediate_select", lambda *args: None)
    monkeypatch.setattr(scheduler, "_record_scheduling_metrics", lambda *args: None)
    monkeypatch.setattr(scheduler, "_record_affinity", lambda *args: None)
    depth = [2]
    monkeypatch.setattr(scheduler._queue_mgr, "get_total_depth_by_model", lambda model_id: depth[0])

    async def dispatch(*args):
        depth[0] = 0
        return SchedulingResult(1, 20, "logosnode", None, True, 0)

    monkeypatch.setattr(scheduler, "_queue_and_wait", AsyncMock(side_effect=dispatch))
    result = await scheduler.schedule(_make_request([(1, 1.0, 5)], []))
    assert result.provider_id == 20
    assert [row["queue_depth"] for row in result.routing_candidates] == [2, 2]
    assert result.routing_candidates[0]["expected_wait_s"] == 45
