"""Deterministic routing under the simple residency strategy."""

import asyncio
from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import pytest

from logos.logosnode_registry import LogosNodeRuntimeRegistry, ProviderSession
from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.pipeline.ettft_estimator import EttftEstimate, ReadinessTier
from logos.pipeline.scheduler_interface import SchedulingRequest
from logos.queue import PriorityQueueManager
from logos.sdi.logosnode_facade import LogosNodeSchedulingDataFacade

from .test_correcting_scheduler import MockLogosNodeFacade, _make_view


@pytest.mark.parametrize(
    "policy,expected", [(None, 10), ("", 10), ("invalid", 10), ("demand", 20), ("lru", 20), ("simple", 10)]
)
def test_simple_ignores_wait_estimates_of_ready_peers(monkeypatch, policy, expected):
    if policy is None:
        monkeypatch.delenv("LOGOS_RESIDENCY_POLICY", raising=False)
    else:
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
    if policy in {"demand", "lru"}:
        scored = [item for item in scored if item[1] != 5]
    assert scheduler._try_immediate_select(scored, "request")[1] == expected


async def test_simple_prefers_idle_lane_and_keeps_transition_exclusions(monkeypatch):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "simple")
    registry = LogosNodeRuntimeRegistry()
    session = ProviderSession(1, "worker", MagicMock())
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
    assert (await registry.select_lane_for_model(1, "model"))["lane_id"] == "c"
    registry.mark_lane_cold(1, "c")
    assert (await registry.select_lane_for_model(1, "model"))["lane_id"] == "b"


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setenv("LOGOS_RESIDENCY_POLICY", "simple")
    registry = LogosNodeRuntimeRegistry()
    queue = PriorityQueueManager()
    facade = LogosNodeSchedulingDataFacade(queue, db_manager=MagicMock(), runtime_registry=registry)
    scheduler = ClassificationCorrectingScheduler(
        queue, facade, MagicMock(), {(1, 10): "logosnode", (1, 20): "logosnode"}
    )
    for pid, ids in [(10, ["a", "b"]), (20, ["c"])]:
        registry._sessions[pid] = ProviderSession(
            pid,
            str(pid),
            MagicMock(),
            capabilities_models={"model"},
            latest_runtime={
                "lanes": [
                    {"lane_id": lid, "model": "model", "runtime_state": "loaded", "num_parallel": 4} for lid in ids
                ]
            },
        )
        facade.register_model(1, str(pid), model_name="model", provider_id=pid)
    return scheduler, registry


def request(rid, *, pids=(10, 20), pin=None, prefix=None):
    return SchedulingRequest(
        request_id=rid,
        payload={"model": "model", "input": "hello"},
        classified_models=[(1, 1.0, 5)],
        deployments=[{"model_id": 1, "provider_id": pid, "type": "logosnode"} for pid in pids],
        required_provider_id=pin,
        affinity_keys=[prefix] if prefix else None,
        timeout_s=2,
    )


@pytest.mark.parametrize("prefix", [None, "shared-stream"])
async def test_completed_requests_share_three_warm_lanes(cluster, prefix):
    scheduler, registry = cluster
    choices = []
    # A prefix already cached by the single-replica peer wins a fresh round.
    if prefix:
        scheduler._prefix_router.record(1, [prefix], 20)
    for i in range(30):
        rid = f"sequential-{i}"
        result = await scheduler.schedule(request(rid, prefix=prefix))
        pid = result.provider_id
        lane = await registry.select_lane_for_model(pid, "model", request_id=rid)
        choices.append(lane["lane_id"])
        scheduler.release(1, pid, "logosnode", rid)
        await registry.update_runtime(pid, registry._sessions[pid].latest_runtime)
    assert Counter(choices) == {"a": 10, "b": 10, "c": 10}
    if prefix:
        assert choices[0] == "c"


async def test_burst_reserves_capacity_proportionally_before_lane_resolution(cluster):
    scheduler, registry = cluster
    registry._sessions[10].latest_runtime["lanes"][0]["num_parallel"] = 2
    registry._sessions[20].latest_runtime["lanes"][0]["num_parallel"] = 6
    requests = [request(f"burst-{i}") for i in range(12)]
    results = await asyncio.gather(*(scheduler.schedule(req) for req in requests))
    # No heartbeat or resolver ran between reservations. A status sampled before
    # the sends must not make the orchestrator forget those in-flight bookings.
    for pid, session in registry._sessions.items():
        await registry.update_runtime(pid, session.latest_runtime)
    assigned = []
    for req, result in reversed(list(zip(requests, results))):
        lane = await registry.select_lane_for_model(result.provider_id, "model", request_id=req.request_id)
        assert await registry.select_lane_for_model(result.provider_id, "model", request_id=req.request_id) == lane
        assigned.append(lane["lane_id"])
    assert Counter(assigned) == {"a": 2, "b": 4, "c": 6}
    for req, result in zip(requests, results):
        scheduler.release(1, result.provider_id, "logosnode", req.request_id)
    assert all(not session.warm_bookings.requests for session in registry._sessions.values())


async def test_candidate_probes_do_not_consume_turns_and_filtered_lanes_do_not_admit(cluster):
    scheduler, registry = cluster
    lanes = registry._sessions[10].latest_runtime["lanes"]
    lanes[0]["transition"] = "draining"
    registry.mark_lane_cold(10, "b")
    for lid, state, props in [
        ("sleep", "loaded", {"sleep_state": "sleeping"}),
        ("start", "starting", {}),
        ("other", "loaded", {"model": "other"}),
    ]:
        lanes.append({"lane_id": lid, "model": "model", "runtime_state": state, **props})
    assert await registry.select_lane_for_model(10, "model") is None
    assert not scheduler._logosnode.evaluate_admission(1, 10).can_admit
    for _ in range(3):
        assert (await registry.select_lane_for_model(20, "model"))["lane_id"] == "c"
    assert not registry._sessions[20].warm_bookings.rounds
    result = await scheduler.schedule(request("eligible"))
    assert result.provider_id == 20
    scheduler.release(1, 20, "logosnode", "eligible")


async def test_queue_reserves_each_turn_and_preserves_filtered_and_pinned_providers(cluster):
    scheduler, registry = cluster
    for pid, session in registry._sessions.items():
        for lane in session.latest_runtime["lanes"]:
            lane["backend_metrics"] = {"queue_waiting": 1}
        await registry.update_runtime(pid, session.latest_runtime)
    requests = [request("filtered", pids=(20,)), request("pinned", pin=20)] + [request(f"queued-{i}") for i in range(4)]
    tasks = [asyncio.create_task(scheduler.schedule(req)) for req in requests]
    await asyncio.sleep(0)
    assert scheduler.get_total_queue_depth() == 6
    for pid, session in registry._sessions.items():
        for lane in session.latest_runtime["lanes"]:
            lane["backend_metrics"] = {}
        await registry.update_runtime(pid, session.latest_runtime)
    scheduler.reevaluate_model_queues("model")
    results = await asyncio.gather(*tasks)
    assert all(result.was_queued for result in results)
    assert [r.provider_id for r in results[:2]] == [20, 20]
    choices = [
        (await registry.select_lane_for_model(result.provider_id, "model", request_id=req.request_id))["lane_id"]
        for req, result in zip(requests, results)
    ]
    assert Counter(choices) == {"a": 2, "b": 2, "c": 2}
    for req, result in zip(requests, results):
        scheduler.release(1, result.provider_id, "logosnode", req.request_id)


async def test_long_request_waits_for_room_on_the_fitting_lane(cluster):
    scheduler, registry = cluster
    session = registry._sessions[10]
    narrow, wide = session.latest_runtime["lanes"]
    narrow["context_length"] = 8192
    wide["context_length"] = 65536
    wide["backend_metrics"] = {"queue_waiting": 1}
    req = request("long", pids=(10,))
    pending = asyncio.create_task(scheduler.schedule(req))
    await asyncio.sleep(0)
    scheduler.reevaluate_model_queues("model")
    assert scheduler.get_total_queue_depth() == 1
    wide["backend_metrics"] = {}
    await registry.update_runtime(10, session.latest_runtime)
    scheduler.on_worker_report(10)
    result = await pending
    assert result.was_queued
    assert (await registry.select_lane_for_model(10, "model", request_id=req.request_id))["lane_id"] == wide["lane_id"]
    scheduler.release(1, 10, "logosnode", req.request_id)
