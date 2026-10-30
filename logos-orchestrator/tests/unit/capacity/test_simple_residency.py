"""Simple sleep-aware planning: FIFO activation, LRU victims and no preemption."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from logos import CapacityPlanner
from logos.queue import Priority, PriorityQueueManager
from logos.sdi.logosnode_facade import LogosNodeSchedulingDataFacade

from .test_best_first_scenarios import _lane
from .test_queued_residency_fairness import _MockProvider, _planner, _profile, _two_gpu_snapshot
from .test_residency_policy import _evict_one, _setup


def _simple(provider, scores=None, arrivals=None):
    planner = _planner(provider, scores or {}, snapshot=_two_gpu_snapshot(free0=48_000, free1=48_000))
    planner._residency_policy = "simple"
    planner._facade.get_oldest_queued_at_by_model_name = lambda model, pid: (arrivals or {}).get(model)
    return planner


@pytest.mark.parametrize("raw", ["simple", "SIMPLE", " simple "])
def test_accepts_simple(raw):
    assert CapacityPlanner._parse_residency_policy(raw) == "simple"


def test_simple_uses_lru_victims():
    planner, provider = _setup("simple")
    assert _evict_one(planner, provider) == "Hot"


def test_fifo_beats_queue_size_and_historical_demand():
    provider = _MockProvider(
        1,
        "worker",
        profiles={m: _profile(40_000) for m in ["old", "new"]},
        capabilities=["old", "new"],
        available_vram_mb=96_000,
        queue_depths={"old": 1, "new": 20},
    )
    planner = _simple(provider, {"old": 0.0, "new": 100.0}, {"old": 10, "new": 20})
    actions = planner._compute_demand_actions(1, [])
    assert [a.model_name for a in actions if a.action == "load"] == ["old", "new"]


def test_fifo_cycle_does_not_reuse_stale_victims():
    provider = _MockProvider(
        1,
        "worker",
        profiles={m: _profile(40_000) for m in ["old", "new"]},
        capabilities=["new", "old"],
        available_vram_mb=96_000,
        queue_depths={"old": 1, "new": 20},
    )
    planner = _simple(provider, {"new": 100.0}, {"old": 10, "new": 20})
    actions = planner._compute_simple_cycle_actions([1], set(), {}, {})
    assert [a.model_name for a in actions if a.action == "load"] == ["old"]


def test_simple_does_not_load_from_historical_demand_alone():
    provider = _MockProvider(
        1, "worker", profiles={"model": _profile(40_000)}, capabilities=["model"], available_vram_mb=96_000
    )
    planner = _simple(provider, {"model": 100.0})
    assert planner._compute_demand_actions(1, []) == []


def test_simple_does_not_add_demand_replicas():
    lane = _lane(lane_id="a", model_name="model")
    provider = _MockProvider(
        1,
        "worker",
        lanes=[lane],
        profiles={"model": _profile(40_000)},
        capabilities=["model"],
        available_vram_mb=96_000,
        queue_depths={"model": 20},
    )
    planner = _simple(provider, {"model": 100.0}, {"model": 10})
    assert planner._compute_demand_actions(1, [lane]) == []


def test_simple_never_initiates_busy_drain():
    planner = CapacityPlanner.__new__(CapacityPlanner)
    planner._residency_policy = "simple"
    assert planner._compute_demand_drain_actions(1, []) == []
    assert not planner._should_initiate_drain(1, None, None, {})


@pytest.mark.parametrize("field", ["active_requests", "requests_running", "queue_waiting"])
def test_final_sleep_check_refuses_busy_lane(field):
    planner = CapacityPlanner.__new__(CapacityPlanner)
    lane = {"lane_id": "a", "active_requests": 0, "backend_metrics": {}}
    if field == "active_requests":
        lane[field] = 1
    else:
        lane["backend_metrics"][field] = 1
    planner._registry = MagicMock()
    planner._registry.peek_runtime_snapshot.return_value = {"runtime": {"lanes": [lane]}}
    assert not planner._lane_is_idle_now(1, "a")


def test_final_sleep_check_accepts_idle_lane_and_refuses_missing_snapshot():
    planner = CapacityPlanner.__new__(CapacityPlanner)
    planner._registry = MagicMock()
    planner._registry.peek_runtime_snapshot.return_value = {"runtime": {"lanes": [{"lane_id": "a"}]}}
    assert planner._lane_is_idle_now(1, "a")
    planner._registry.peek_runtime_snapshot.return_value = None
    assert not planner._lane_is_idle_now(1, "a")


def test_first_waiting_uses_arrival_instead_of_priority_and_respects_affinity():
    queue = PriorityQueueManager()
    first = queue.enqueue(None, 1, priority=Priority.LOW)
    pinned = queue.enqueue(None, 1, priority=Priority.HIGH, provider_affinity=2)
    last = queue.enqueue(None, 1, priority=Priority.HIGH)
    for entry, at in [(first, 10), (pinned, 5), (last, 20)]:
        queue.get_entry_info(entry).enqueue_time = datetime.fromtimestamp(at)
    facade = LogosNodeSchedulingDataFacade.__new__(LogosNodeSchedulingDataFacade)
    facade.queue_manager = queue
    facade._providers = {
        1: SimpleNamespace(_model_id_to_name={1: "model"}),
        2: SimpleNamespace(_model_id_to_name={1: "model"}),
    }
    planner = CapacityPlanner.__new__(CapacityPlanner)
    planner._facade = facade
    assert planner._first_waiting_at(1, "model") == 10
    assert planner._first_waiting_at(2, "model") == 5
    queue.remove(first)
    assert planner._first_waiting_at(1, "model") == 20
    queue.remove(last)
    assert planner._first_waiting_at(1, "model") == float("inf")


@pytest.mark.parametrize("active", [0, 1])
def test_simple_preserves_queued_capacity_safety(monkeypatch, active):
    from logos.capacity import capacity_planner as module

    from .test_queued_residency_fairness import COLD, FINISHED, _contention_scenario

    monkeypatch.setattr(module.time, "time", lambda: 10_000.0)
    planner, provider = _contention_scenario(
        finished_score=0.58, cold_score=0.34, cold_queue=1, now=10_000.0, other_active_requests=active
    )
    planner._residency_policy = "simple"
    planner._facade.get_oldest_queued_at_by_model_name = lambda model, pid: 9_900 if model == COLD else None
    actions = planner._validate_vram_budget(planner._compute_demand_actions(1, provider.lanes))
    kinds = [(a.action, a.model_name) for a in actions]
    assert ("wake", FINISHED) not in kinds
    assert (("load", COLD) in kinds) == (active == 0)


@pytest.mark.parametrize("sleeping", [False, True])
def test_simple_bypasses_contention_ratio_for_waiting_request(monkeypatch, sleeping):
    from logos.capacity import capacity_planner as module

    from .test_queued_residency_fairness import COLD, VICTIM_A, VICTIM_B
    from .test_queued_residency_fairness import _lane as fairness_lane
    from .test_residency_policy import TestLruRatioBypass

    monkeypatch.setattr(module.time, "time", lambda: 10_000.0)
    provider = TestLruRatioBypass()._sum_gate_provider()
    provider.queue_depths[COLD] = 1
    if sleeping:
        provider.lanes.append(
            fairness_lane(
                lane_id="target",
                model_name=COLD,
                runtime_state="sleeping",
                sleep_state="sleeping",
                effective_vram_mb=500,
                gpu_devices="0,1",
            )
        )
    planner = _planner(
        provider, scores={COLD: 0.0, VICTIM_A: 1.5, VICTIM_B: 1.5}, snapshot=_two_gpu_snapshot(free0=5_000, free1=5_000)
    )
    planner._residency_policy = "simple"
    planner._facade.get_oldest_queued_at_by_model_name = lambda model, pid: 9_900 if model == COLD else None
    for lane in provider.lanes:
        planner._lane_loaded_at[(1, lane.lane_id)] = 9_000
    actions = planner._compute_demand_actions(1, provider.lanes)
    assert ("wake" if sleeping else "load", COLD) in [(a.action, a.model_name) for a in actions]


def test_simple_retains_tenure_and_failed_wake_backoff(monkeypatch):
    from logos.capacity import capacity_planner as module

    from .test_queued_residency_fairness import COLD, FINISHED, _contention_scenario

    monkeypatch.setattr(module.time, "time", lambda: 10_000.0)
    planner, provider = _contention_scenario(finished_score=0.58, cold_score=0.34, cold_queue=1, now=10_000.0)
    planner._residency_policy = "simple"
    planner._facade.get_oldest_queued_at_by_model_name = lambda model, pid: 9_900 if model == COLD else None
    planner._lane_loaded_at[(1, "lane-other")] = 9_999
    assert not any(a.action == "load" for a in planner._compute_demand_actions(1, provider.lanes))
    provider.queue_depths = {FINISHED: 1}
    planner._lane_wake_failure_until[(1, "lane-finished")] = 10_010
    assert not any(a.action == "wake" for a in planner._compute_demand_actions(1, provider.lanes))


def test_simple_fifo_across_workers_does_not_follow_provider_iteration_order():
    from .test_best_first_scenarios import _MockFacade
    from .test_best_first_scenarios import _MockProvider as Provider

    first = Provider(
        provider_id=1, name="first", capabilities=["new"], profiles={"new": _profile(40_000)}, available_vram_mb=96_000
    )
    second = Provider(
        provider_id=2, name="second", capabilities=["old"], profiles={"old": _profile(40_000)}, available_vram_mb=96_000
    )
    planner = _simple(_MockProvider(1, "worker"))
    planner._facade = _MockFacade([first, second])
    planner._facade.get_scheduler_queue_depth_by_model_name = lambda model, pid: int(
        (pid, model) in {(1, "new"), (2, "old")}
    )
    planner._facade.get_oldest_queued_at_by_model_name = lambda model, pid: {"old": 10, "new": 20}[model]
    actions = planner._compute_simple_cycle_actions([1, 2], set(), {}, {})
    assert [(a.model_name, a.provider_id) for a in actions if a.action == "load"] == [("old", 2), ("new", 1)]
