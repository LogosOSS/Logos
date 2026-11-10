"""Deterministic routing under the simple residency strategy."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from logos.logosnode_registry import LogosNodeRuntimeRegistry
from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.pipeline.ettft_estimator import EttftEstimate, ReadinessTier
from logos.queue import PriorityQueueManager

from .test_correcting_scheduler import MockLogosNodeFacade, _make_view


@pytest.mark.parametrize("policy,expected", [("demand", 20), ("lru", 20), ("simple", 10)])
def test_named_model_routing_uses_fixed_ready_order_only_for_simple(monkeypatch, policy, expected):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", policy)
    facade = MockLogosNodeFacade()
    for pid in [10, 20, 5]:
        facade.set_view(1, pid, _make_view(provider_id=pid))
        facade.set_reserve(1, pid, True)
    scheduler = ClassificationCorrectingScheduler(PriorityQueueManager(), facade, MagicMock())
    estimates = {
        10: EttftEstimate(100, ReadinessTier.WARM, "warm"),
        20: EttftEstimate(1, ReadinessTier.WARM, "warm"),
        5: EttftEstimate(0.1, ReadinessTier.SLEEPING, "sleeping"),
    }
    scheduler._estimate_ettft = lambda mid, pid, *a, **k: estimates[pid]
    scored = scheduler._compute_candidate_scores(
        [(1, 1.0, 5)], [{"model_id": 1, "provider_id": pid, "type": "logosnode"} for pid in [20, 10, 5]]
    )
    # Under demand/lru the sleeping candidate would legitimately win by its estimate.
    if policy != "simple":
        scored = [item for item in scored if item[1] != 5]
    assert scheduler._try_immediate_select(scored, "request")[1] == expected


async def test_simple_selects_lane_id_and_keeps_transition_exclusions(monkeypatch):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "simple")
    registry = LogosNodeRuntimeRegistry()
    session = MagicMock()
    session.capabilities_models = ["model"]
    session.latest_runtime = {
        "lanes": [
            {"lane_id": "c", "model": "model", "runtime_state": "loaded", "active_requests": 0},
            {"lane_id": "b", "model": "model", "runtime_state": "running", "active_requests": 4},
            {"lane_id": "a", "model": "model", "runtime_state": "running", "transition": "draining"},
            {"lane_id": "0", "model": "model", "runtime_state": "starting"},
        ]
    }
    registry._get_active_session = AsyncMock(return_value=session)
    assert (await registry.select_lane_for_model(1, "model"))["lane_id"] == "b"
    registry.mark_lane_cold(1, "b")
    assert (await registry.select_lane_for_model(1, "model"))["lane_id"] == "c"
