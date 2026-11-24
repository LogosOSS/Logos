"""The same startup policy drives routing, residency and lane selection."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from logos.capacity.capacity_planner import CapacityPlanner
from logos.logosnode_registry import LogosNodeRuntimeRegistry, ProviderSession
from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.queue import PriorityQueueManager
from logos.residency_policy import configured_residency_policy


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "simple"),
        ("", "simple"),
        ("  ", "simple"),
        ("invalid", "simple"),
        ("simple", "simple"),
        ("demand", "demand"),
        ("lru", "lru"),
        (" SIMPLE ", "simple"),
        (" DEMAND ", "demand"),
        (" LRU ", "lru"),
    ],
)
async def test_components_share_startup_policy(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LOGOS_RESIDENCY_POLICY", raising=False)
    else:
        monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", raw)
    registry = LogosNodeRuntimeRegistry()
    scheduler = ClassificationCorrectingScheduler(PriorityQueueManager(), MagicMock(), MagicMock())
    planner = CapacityPlanner(MagicMock(), registry, MagicMock())
    assert configured_residency_policy() == expected
    assert planner.residency_policy == scheduler._residency_policy == registry._residency_policy == expected

    # Existing components retain their selection until they are restarted.
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "demand" if expected == "simple" else "simple")
    assert planner.residency_policy == expected
    assert planner._uses_demand_ordering() == scheduler._uses_ettft_routing() == (expected != "simple")
    assert planner._drains_before_sleep() == (expected != "simple")
    assert await _selected_lane(registry) == ("b" if expected == "simple" else "a")


async def _selected_lane(registry):
    session = ProviderSession(1, "worker", MagicMock())
    session.capabilities_models = ["model"]
    session.latest_runtime = {
        "lanes": [
            {"lane_id": "b", "model": "model", "runtime_state": "loaded", "active_requests": 2, "num_parallel": 8},
            {"lane_id": "a", "model": "model", "runtime_state": "running", "active_requests": 1, "num_parallel": 1},
        ]
    }
    registry._get_active_session = AsyncMock(return_value=session)
    return (await registry.select_lane_for_model(1, "model"))["lane_id"]


async def test_missing_component_attributes_use_simple(monkeypatch):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "demand")
    scheduler = ClassificationCorrectingScheduler.__new__(ClassificationCorrectingScheduler)
    planner = CapacityPlanner.__new__(CapacityPlanner)
    registry = LogosNodeRuntimeRegistry()
    del registry._residency_policy
    assert planner.residency_policy == "simple"
    assert not planner._uses_demand_ordering()
    assert not planner._drains_before_sleep()
    assert not scheduler._uses_ettft_routing()
    assert await _selected_lane(registry) == "b"


def test_invalid_policy_warning_names_simple(monkeypatch, caplog):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "invalid")
    assert configured_residency_policy() == "simple"
    assert "falling back to 'simple'" in caplog.text
