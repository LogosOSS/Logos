"""Prefix-cache-aware placement in the correcting scheduler.

Multi-model RESOURCE ranking may still nudge a warm sticky worker with a
bounded corrected-score bonus. Named-model peer ranking orders by true
expected wait and does not apply that bonus (see
test_named_model_ettft_ranking.py).
"""

import pytest
from tests.unit.pipeline.test_correcting_scheduler import MockAzureFacade, MockLogosNodeFacade, _make_view

from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.pipeline.prefix_affinity import PrefixAffinityRouter
from logos.pipeline.scheduler_interface import SchedulingRequest
from logos.queue import PriorityQueueManager

MODEL_ID = 1
OTHER_MODEL_ID = 2
WORKER_A = 10
WORKER_B = 11
KEYS = ["deep-block", "shallow-block"]

DEPLOYMENTS = [
    {"model_id": MODEL_ID, "provider_id": WORKER_A, "type": "logosnode"},
    {"model_id": MODEL_ID, "provider_id": WORKER_B, "type": "logosnode"},
    {"model_id": OTHER_MODEL_ID, "provider_id": WORKER_A, "type": "logosnode"},
]

# Two classified models → multi-model RESOURCE path (affinity bonus applies).
MULTI_MODEL_CANDIDATES = [(MODEL_ID, 10.0, 1), (OTHER_MODEL_ID, 9.0, 1)]


def _scheduler(logosnode, router=None):
    return ClassificationCorrectingScheduler(
        queue_manager=PriorityQueueManager(),
        logosnode_facade=logosnode,
        azure_facade=MockAzureFacade(),
        prefix_router=router if router is not None else PrefixAffinityRouter(ttl_s=600, max_entries=100),
    )


def _two_warm_workers(queue_waiting_a=0.0, queue_waiting_b=0.0):
    logosnode = MockLogosNodeFacade()
    logosnode.set_view(
        MODEL_ID,
        WORKER_A,
        _make_view(model_id=MODEL_ID, provider_id=WORKER_A, aggregate_queue_waiting=queue_waiting_a),
    )
    logosnode.set_view(
        MODEL_ID,
        WORKER_B,
        _make_view(model_id=MODEL_ID, provider_id=WORKER_B, aggregate_queue_waiting=queue_waiting_b),
    )
    logosnode.set_view(
        OTHER_MODEL_ID,
        WORKER_A,
        _make_view(model_id=OTHER_MODEL_ID, provider_id=WORKER_A, model_name="other"),
    )
    return logosnode


def _request(keys=KEYS, request_id="req-1"):
    return SchedulingRequest(
        request_id=request_id,
        classified_models=MULTI_MODEL_CANDIDATES,
        deployments=DEPLOYMENTS,
        payload={},
        affinity_keys=keys,
    )


# ---------------------------------------------------------------------------
# Placement (multi-model RESOURCE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_returns_to_the_worker_that_last_served_it():
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    router.record(MODEL_ID, KEYS, WORKER_B)
    scheduler = _scheduler(_two_warm_workers(), router)

    # Repeated so a lucky coin flip cannot pass for affinity.
    for index in range(10):
        result = await scheduler.schedule(_request(request_id=f"req-{index}"))
        assert result is not None
        assert result.provider_id == WORKER_B


@pytest.mark.asyncio
async def test_without_affinity_keys_placement_is_unchanged():
    """No keys → no lookup, no bonus: the tie-break stays random."""
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    router.record(MODEL_ID, KEYS, WORKER_B)
    scheduler = _scheduler(_two_warm_workers(), router)

    picked = {(await scheduler.schedule(_request(keys=None, request_id=f"r{i}"))).provider_id for i in range(40)}
    assert picked == {WORKER_A, WORKER_B}


@pytest.mark.asyncio
async def test_a_meaningfully_faster_peer_still_wins():
    """Affinity is worth ~15s of expected wait, not an unconditional pin."""
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    router.record(MODEL_ID, KEYS, WORKER_A)
    # Worker A is deeply backlogged; worker B is idle.
    scheduler = _scheduler(_two_warm_workers(queue_waiting_a=400.0), router)

    result = await scheduler.schedule(_request())
    assert result.provider_id == WORKER_B


@pytest.mark.asyncio
async def test_a_marginally_faster_peer_does_not_break_the_stream():
    """A small wait difference is cheaper to absorb than a cold prefix cache."""
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    router.record(MODEL_ID, KEYS, WORKER_A)
    scheduler = _scheduler(_two_warm_workers(queue_waiting_a=2.0), router)

    result = await scheduler.schedule(_request())
    assert result.provider_id == WORKER_A


def test_no_bonus_for_a_worker_whose_lane_is_not_warm():
    """Stickiness must never wake a sleeping worker or trigger a cold load."""
    logosnode = MockLogosNodeFacade()
    logosnode.set_view(
        MODEL_ID,
        WORKER_A,
        _make_view(model_id=MODEL_ID, provider_id=WORKER_A, best_lane_state="sleeping", is_loaded=False),
    )
    logosnode.set_view(MODEL_ID, WORKER_B, _make_view(model_id=MODEL_ID, provider_id=WORKER_B))
    logosnode.set_view(
        OTHER_MODEL_ID,
        WORKER_A,
        _make_view(model_id=OTHER_MODEL_ID, provider_id=WORKER_A, model_name="other"),
    )
    scheduler = _scheduler(logosnode)

    with_affinity = scheduler._compute_candidate_scores(MULTI_MODEL_CANDIDATES, DEPLOYMENTS, {MODEL_ID: WORKER_A})
    without_affinity = scheduler._compute_candidate_scores(MULTI_MODEL_CANDIDATES, DEPLOYMENTS, {})

    scores_with = {provider_id: score for _m, provider_id, _t, score, _p, _e in with_affinity if _m == MODEL_ID}
    scores_without = {provider_id: score for _m, provider_id, _t, score, _p, _e in without_affinity if _m == MODEL_ID}
    assert scores_with[WORKER_A] == scores_without[WORKER_A]


def test_bonus_is_bounded_by_the_weight_span():
    """The bonus must stay on the same scale as the ETTFT penalty, otherwise
    it would override classification, not just the tie-break."""
    from logos.pipeline.correcting_scheduler import PREFIX_AFFINITY_BONUS_FRACTION
    from logos.pipeline.ettft_estimator import CORRECTION_STRENGTH, compute_weight_span

    scheduler = _scheduler(_two_warm_workers())
    scored = scheduler._compute_candidate_scores(MULTI_MODEL_CANDIDATES, DEPLOYMENTS, {MODEL_ID: WORKER_A})
    scores = {provider_id: score for _m, provider_id, _t, score, _p, _e in scored if _m == MODEL_ID}

    expected = compute_weight_span([10.0, 9.0]) * CORRECTION_STRENGTH * PREFIX_AFFINITY_BONUS_FRACTION
    assert scores[WORKER_A] - scores[WORKER_B] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placement_is_recorded_so_the_next_turn_can_follow():
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    scheduler = _scheduler(_two_warm_workers(), router)

    first = await scheduler.schedule(_request())
    assert router.lookup(MODEL_ID, KEYS) == first.provider_id

    second = await scheduler.schedule(_request(request_id="req-2"))
    assert second.provider_id == first.provider_id


@pytest.mark.asyncio
async def test_cloud_placements_are_not_recorded():
    """Cloud upstreams route internally — pinning them buys nothing."""
    logosnode = MockLogosNodeFacade()
    azure = MockAzureFacade()
    router = PrefixAffinityRouter(ttl_s=600, max_entries=100)
    scheduler = ClassificationCorrectingScheduler(
        queue_manager=PriorityQueueManager(),
        logosnode_facade=logosnode,
        azure_facade=azure,
        prefix_router=router,
    )
    request = SchedulingRequest(
        request_id="req-cloud",
        classified_models=[(MODEL_ID, 10.0, 1)],
        deployments=[{"model_id": MODEL_ID, "provider_id": 99, "type": "cloud"}],
        payload={},
        affinity_keys=KEYS,
    )

    result = await scheduler.schedule(request)
    assert result.provider_type == "cloud"
    assert router.lookup(MODEL_ID, KEYS) is None
