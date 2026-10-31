"""Named-model peer ranking: ascending true expected_wait_s.

When the classified candidate list names exactly one model and ETTFT is
enabled, same-model deployments are ordered by true expected wait. The
range-scaled corrected score is not used for that peer order, so waits
above the normalization horizon (61s vs 600s) stay distinct. Multi-model
RESOURCE classification and ETTFT-disabled ablation keep their prior
policies. Affinity must not promote a higher true wait in named-model mode.
"""

from __future__ import annotations

import pytest
from tests.unit.pipeline.test_correcting_scheduler import MockLogosNodeFacade, _make_scheduler, _make_view

from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler
from logos.pipeline.ettft_estimator import (
    NORMALIZATION_HORIZON_S,
    OVERHEAD_COLD_S,
    EttftEstimate,
    ReadinessTier,
    compute_corrected_score,
    compute_weight_span,
)
from logos.pipeline.scheduler_interface import SchedulingRequest

EQUAL_WEIGHT = 1.0
WAIT_NEAR = 61.0
WAIT_FAR = 600.0
MODEL_ID = 1
OTHER_MODEL_ID = 2
PROVIDER_NEAR = 10
PROVIDER_FAR = 20


def _busy(wait_s: float) -> EttftEstimate:
    return EttftEstimate(
        expected_wait_s=wait_s,
        tier=ReadinessTier.BUSY,
        reasoning="fixed-wait fixture",
        queue_wait_s=wait_s,
        warmth_state=1,
    )


def _unavailable(wait_s: float) -> EttftEstimate:
    return EttftEstimate(
        expected_wait_s=wait_s,
        tier=ReadinessTier.UNAVAILABLE,
        reasoning="unavailable fixture",
        warmth_state=-1,
    )


def _two_provider_facade():
    logosnode = MockLogosNodeFacade()
    for pid in (PROVIDER_NEAR, PROVIDER_FAR):
        logosnode.set_view(MODEL_ID, pid, _make_view(model_id=MODEL_ID, provider_id=pid))
    return logosnode


def _deployments(provider_order=(PROVIDER_FAR, PROVIDER_NEAR)):
    return [{"model_id": MODEL_ID, "provider_id": pid, "type": "logosnode"} for pid in provider_order]


def _pin_waits(scheduler, waits: dict[int, float], *, unavailable: bool = False):
    factory = _unavailable if unavailable else _busy

    def estimate(model_id, provider_id, provider_type, input_tokens=0, request_id=None):
        return factory(waits[provider_id])

    scheduler._estimate_ettft = estimate  # type: ignore[method-assign]


class TestCorrectedScoreSaturationHelpers:
    """Formula still saturates; named-model ranking must not rely on it for peers."""

    def test_waits_above_horizon_share_corrected_score(self):
        span = compute_weight_span([EQUAL_WEIGHT, EQUAL_WEIGHT])
        assert compute_corrected_score(EQUAL_WEIGHT, WAIT_NEAR, span) == compute_corrected_score(
            EQUAL_WEIGHT, WAIT_FAR, span
        )
        assert WAIT_NEAR > NORMALIZATION_HORIZON_S


class TestNamedModelStrictWaitOrder:
    def test_saturation_61_before_600_reverse_input_order(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR)),
        )
        assert [entry[5].expected_wait_s for entry in scored] == [WAIT_NEAR, WAIT_FAR]
        assert [entry[1] for entry in scored] == [PROVIDER_NEAR, PROVIDER_FAR]

    def test_saturation_600_then_61_input_still_ranks_61_first(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_NEAR, PROVIDER_FAR)),
        )
        assert [entry[1] for entry in scored] == [PROVIDER_NEAR, PROVIDER_FAR]

    @pytest.mark.asyncio
    async def test_immediate_reservation_attempts_lower_wait_first(self):
        logosnode = _two_provider_facade()
        logosnode.set_reserve(MODEL_ID, PROVIDER_NEAR, True)
        logosnode.set_reserve(MODEL_ID, PROVIDER_FAR, True)
        scheduler = _make_scheduler(logosnode=logosnode)
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})

        attempt_order: list[int] = []
        original = logosnode.try_reserve_capacity

        def tracking(model_id, provider_id, request_id):
            attempt_order.append(provider_id)
            return original(model_id, provider_id, request_id)

        logosnode.try_reserve_capacity = tracking  # type: ignore[method-assign]

        result = await scheduler.schedule(
            SchedulingRequest(
                request_id="named-immediate",
                payload={},
                deployments=_deployments((PROVIDER_FAR, PROVIDER_NEAR)),
                classified_models=[(MODEL_ID, EQUAL_WEIGHT, 5)],
            )
        )
        assert result is not None
        assert result.provider_id == PROVIDER_NEAR
        assert attempt_order[0] == PROVIDER_NEAR

    @pytest.mark.asyncio
    async def test_queue_representative_prefers_61_when_both_unreservable(self):
        logosnode = _two_provider_facade()
        logosnode.set_reserve(MODEL_ID, PROVIDER_NEAR, False)
        logosnode.set_reserve(MODEL_ID, PROVIDER_FAR, False)
        scheduler = _make_scheduler(logosnode=logosnode)
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})

        captured: dict = {}

        async def fake_queue(best_scored, request):
            captured["provider_id"] = best_scored[1]
            captured["wait"] = best_scored[5].expected_wait_s
            return None

        scheduler._queue_and_wait = fake_queue  # type: ignore[method-assign]

        await scheduler.schedule(
            SchedulingRequest(
                request_id="named-queue",
                payload={},
                deployments=_deployments((PROVIDER_FAR, PROVIDER_NEAR)),
                classified_models=[(MODEL_ID, EQUAL_WEIGHT, 5)],
            )
        )
        assert captured["provider_id"] == PROVIDER_NEAR
        assert captured["wait"] == WAIT_NEAR

    def test_unavailable_fallback_representative_prefers_lower_true_wait(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(
            scheduler,
            {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR},
            unavailable=True,
        )
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR)),
        )
        assert len(scored) == 2
        assert scored[0][1] == PROVIDER_NEAR
        assert scored[0][5].expected_wait_s == WAIT_NEAR
        assert scored[1][5].expected_wait_s == WAIT_FAR

    def test_true_equal_ettft_ties_on_ranking_key(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: 12.0, PROVIDER_FAR: 12.0})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments(),
        )
        assert abs(scored[0][3] - scored[1][3]) <= scheduler._TIE_EPSILON
        shuffled = scheduler._weighted_shuffle_tied(list(scored))
        assert {entry[1] for entry in shuffled} == {PROVIDER_NEAR, PROVIDER_FAR}


class TestNamedModelAffinityCannotOutrankFasterWait:
    def test_affinity_bonus_does_not_promote_higher_wait_peer(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR)),
            affinity={MODEL_ID: PROVIDER_FAR},
        )
        assert scored[0][1] == PROVIDER_NEAR
        assert scored[0][5].expected_wait_s == WAIT_NEAR


class TestControlsUnchanged:
    def test_ettft_disabled_ignores_wait_order(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade(), ettft_enabled=False)
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR)),
        )
        # Ablation: ranking key is the classification weight for every peer.
        assert scored[0][3] == EQUAL_WEIGHT
        assert scored[1][3] == EQUAL_WEIGHT

    def test_multi_model_resource_keeps_corrected_score_and_affinity(self):
        logosnode = _two_provider_facade()
        logosnode.set_view(
            OTHER_MODEL_ID,
            PROVIDER_NEAR,
            _make_view(model_id=OTHER_MODEL_ID, provider_id=PROVIDER_NEAR),
        )
        scheduler = _make_scheduler(logosnode=logosnode)

        def estimate(model_id, provider_id, provider_type, input_tokens=0, request_id=None):
            waits = {
                (MODEL_ID, PROVIDER_NEAR): 5.0,
                (MODEL_ID, PROVIDER_FAR): 5.0,
                (OTHER_MODEL_ID, PROVIDER_NEAR): 5.0,
            }
            return EttftEstimate(
                expected_wait_s=waits[(model_id, provider_id)],
                tier=ReadinessTier.WARM,
                reasoning="warm fixture",
                warmth_state=0,
            )

        scheduler._estimate_ettft = estimate  # type: ignore[method-assign]
        deployments = _deployments() + [{"model_id": OTHER_MODEL_ID, "provider_id": PROVIDER_NEAR, "type": "logosnode"}]
        with_affinity = scheduler._compute_candidate_scores(
            [(MODEL_ID, 10.0, 1), (OTHER_MODEL_ID, 9.0, 1)],
            deployments,
            affinity={MODEL_ID: PROVIDER_FAR},
        )
        without = scheduler._compute_candidate_scores(
            [(MODEL_ID, 10.0, 1), (OTHER_MODEL_ID, 9.0, 1)],
            deployments,
            affinity={},
        )
        scores_with = {(m, p): s for m, p, _, s, _, _ in with_affinity}
        scores_without = {(m, p): s for m, p, _, s, _, _ in without}
        assert scores_with[(MODEL_ID, PROVIDER_FAR)] > scores_without[(MODEL_ID, PROVIDER_FAR)]
        assert scores_with[(MODEL_ID, PROVIDER_NEAR)] == scores_without[(MODEL_ID, PROVIDER_NEAR)]

    def test_multi_model_does_not_use_negative_wait_ranking_key(self):
        logosnode = _two_provider_facade()
        logosnode.set_view(
            OTHER_MODEL_ID,
            PROVIDER_NEAR,
            _make_view(model_id=OTHER_MODEL_ID, provider_id=PROVIDER_NEAR),
        )
        scheduler = _make_scheduler(logosnode=logosnode)
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR})

        def estimate(model_id, provider_id, provider_type, input_tokens=0, request_id=None):
            if model_id == OTHER_MODEL_ID:
                return _busy(1.0)
            return _busy({PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR}[provider_id])

        scheduler._estimate_ettft = estimate  # type: ignore[method-assign]
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5), (OTHER_MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments() + [{"model_id": OTHER_MODEL_ID, "provider_id": PROVIDER_NEAR, "type": "logosnode"}],
        )
        # Corrected-score path: saturated peers share a ranking key (clipping).
        model_scores = {p: s for m, p, _, s, _, _ in scored if m == MODEL_ID}
        assert abs(model_scores[PROVIDER_NEAR] - model_scores[PROVIDER_FAR]) <= scheduler._TIE_EPSILON

    def test_resource_unavailable_finite_waits_keep_cold_overhead_fallback(self):
        """Multi-model UNAVAILABLE must not preserve estimator waits (61/600)."""
        logosnode = _two_provider_facade()
        logosnode.set_view(
            OTHER_MODEL_ID,
            PROVIDER_NEAR,
            _make_view(model_id=OTHER_MODEL_ID, provider_id=PROVIDER_NEAR),
        )
        scheduler = _make_scheduler(logosnode=logosnode)

        def estimate(model_id, provider_id, provider_type, input_tokens=0, request_id=None):
            if model_id == OTHER_MODEL_ID:
                return _unavailable(1.0)
            return _unavailable({PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: WAIT_FAR}[provider_id])

        scheduler._estimate_ettft = estimate  # type: ignore[method-assign]
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5), (OTHER_MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR))
            + [{"model_id": OTHER_MODEL_ID, "provider_id": PROVIDER_NEAR, "type": "logosnode"}],
        )
        model_entries = [entry for entry in scored if entry[0] == MODEL_ID]
        assert len(model_entries) == 2
        assert {entry[5].expected_wait_s for entry in model_entries} == {OVERHEAD_COLD_S}
        assert {entry[5].state_overhead_s for entry in model_entries} == {OVERHEAD_COLD_S}


class TestRankingScoreTiesAndInfinities:
    def test_equal_negative_infinities_tie_opposite_signs_do_not(self):
        assert ClassificationCorrectingScheduler._ranking_scores_tie(float("-inf"), float("-inf"))
        assert ClassificationCorrectingScheduler._ranking_scores_tie(float("inf"), float("inf"))
        assert not ClassificationCorrectingScheduler._ranking_scores_tie(float("-inf"), float("inf"))
        assert not ClassificationCorrectingScheduler._ranking_scores_tie(float("-inf"), -61.0)

    def test_finite_epsilon_boundary(self):
        eps = ClassificationCorrectingScheduler._TIE_EPSILON
        assert ClassificationCorrectingScheduler._ranking_scores_tie(1.0, 1.0 + eps / 2)
        assert not ClassificationCorrectingScheduler._ranking_scores_tie(1.0, 1.0 + eps)

    def test_named_model_finite_wait_outranks_infinite_busy(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: WAIT_NEAR, PROVIDER_FAR: float("inf")})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments((PROVIDER_FAR, PROVIDER_NEAR)),
        )
        assert scored[0][1] == PROVIDER_NEAR
        assert scored[0][5].expected_wait_s == WAIT_NEAR
        assert scored[1][5].expected_wait_s == float("inf")
        assert scored[1][3] == float("-inf")

    def test_equal_infinite_busy_waits_share_shuffle_zone(self):
        scheduler = _make_scheduler(logosnode=_two_provider_facade())
        _pin_waits(scheduler, {PROVIDER_NEAR: float("inf"), PROVIDER_FAR: float("inf")})
        scored = scheduler._compute_candidate_scores(
            [(MODEL_ID, EQUAL_WEIGHT, 5)],
            _deployments(),
        )
        assert scored[0][3] == float("-inf")
        assert ClassificationCorrectingScheduler._ranking_scores_tie(scored[0][3], scored[1][3])
        shuffled = scheduler._weighted_shuffle_tied(list(scored))
        assert {entry[1] for entry in shuffled} == {PROVIDER_NEAR, PROVIDER_FAR}

    @pytest.mark.asyncio
    async def test_both_unreservable_busy_inf_queues_without_index_error(self):
        logosnode = _two_provider_facade()
        logosnode.set_reserve(MODEL_ID, PROVIDER_NEAR, False)
        logosnode.set_reserve(MODEL_ID, PROVIDER_FAR, False)
        scheduler = _make_scheduler(logosnode=logosnode)
        _pin_waits(scheduler, {PROVIDER_NEAR: float("inf"), PROVIDER_FAR: float("inf")})

        captured: dict = {}

        async def fake_queue(best_scored, request):
            captured["provider_id"] = best_scored[1]
            captured["wait"] = best_scored[5].expected_wait_s
            captured["score"] = best_scored[3]
            return None

        scheduler._queue_and_wait = fake_queue  # type: ignore[method-assign]

        await scheduler.schedule(
            SchedulingRequest(
                request_id="named-inf-queue",
                payload={},
                deployments=_deployments((PROVIDER_FAR, PROVIDER_NEAR)),
                classified_models=[(MODEL_ID, EQUAL_WEIGHT, 5)],
            )
        )
        assert captured["wait"] == float("inf")
        assert captured["score"] == float("-inf")
        assert captured["provider_id"] in {PROVIDER_NEAR, PROVIDER_FAR}
