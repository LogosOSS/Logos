"""Regressions for queued-residency fairness in the capacity planner.

Pins: effective-demand ranking (incl. queue), no healthy-success load
cooldown, failure backoff preserved, SUM (not MAX) for wake/load competitive
ratio, speculative eviction yields to queued waiters, spare/disjoint
proactivity kept, active/queued victims protected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from logos.capacity import capacity_planner as planner_mod
from logos.capacity.capacity_planner import CapacityPlanner
from logos.capacity.demand_tracker import DemandTracker
from logos.sdi.models import CapacityPlanAction, LaneSchedulerSignals

PROVIDER_ID = 1
FINISHED = "org/finished-model"
COLD = "org/cold-model"
OTHER = "org/other-model"
VICTIM_A = "org/victim-a"
VICTIM_B = "org/victim-b"


@dataclass
class _MockProvider:
    provider_id: int
    name: str
    lanes: List[LaneSchedulerSignals] = field(default_factory=list)
    profiles: Dict[str, Any] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    available_vram_mb: float = 0.0
    total_vram_mb: float = 98_000.0
    queue_depths: Dict[str, int] = field(default_factory=dict)


class _MockFacade:
    def __init__(self, provider: _MockProvider):
        self._provider = provider

    def get_all_provider_lane_signals(self, provider_id: int) -> List[LaneSchedulerSignals]:
        return list(self._provider.lanes)

    def get_model_profiles(self, provider_id: int) -> Dict[str, Any]:
        return dict(self._provider.profiles)

    def get_worker_capabilities(self, provider_id: int) -> List[str]:
        return list(self._provider.capabilities)

    def get_capacity_info(self, provider_id: int):
        p = self._provider
        return SimpleNamespace(
            available_vram_mb=p.available_vram_mb,
            total_vram_mb=p.total_vram_mb,
            loaded_models=[],
        )

    def get_provider_name(self, provider_id: int) -> str:
        return self._provider.name

    def provider_ids(self) -> List[int]:
        return [self._provider.provider_id]

    def get_scheduler_queue_depth_by_model_name(self, model_name: str, provider_id: int) -> int:
        return int(self._provider.queue_depths.get(model_name, 0))

    def has_cold_queued_entries_by_model_name(self, model_name: str, provider_id: int) -> bool:
        return self.get_scheduler_queue_depth_by_model_name(model_name, provider_id) > 0

    def get_model_last_activity(self, provider_id: int, model_name: str):
        return None


def _lane(
    *,
    lane_id: str,
    model_name: str,
    runtime_state: str = "loaded",
    sleep_state: str = "awake",
    effective_vram_mb: float = 40_000.0,
    gpu_devices: str = "0",
    active_requests: int = 0,
    queue_waiting: float = 0.0,
) -> LaneSchedulerSignals:
    return LaneSchedulerSignals(
        lane_id=lane_id,
        model_name=model_name,
        runtime_state=runtime_state,
        sleep_state=sleep_state,
        active_requests=active_requests,
        queue_waiting=queue_waiting,
        requests_running=0.0,
        gpu_cache_usage_percent=None,
        ttft_p95_seconds=0.0,
        e2e_latency_p50_seconds=0.0,
        effective_vram_mb=effective_vram_mb,
        num_parallel=0,
        gpu_devices=gpu_devices,
    )


def _profile(
    loaded_vram_mb: float,
    *,
    sleeping_residual_mb: float = 500.0,
    tensor_parallel_size: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        loaded_vram_mb=loaded_vram_mb,
        sleeping_residual_mb=sleeping_residual_mb,
        base_residency_mb=loaded_vram_mb,
        kv_budget_mb=0.0,
        tensor_parallel_size=tensor_parallel_size,
        residency_source="calibrated",
        engine="vllm",
        estimate_base_residency_mb=lambda: loaded_vram_mb,
        enforce_eager_at_calibration=None,
    )


def _two_gpu_snapshot(*, free0: float, free1: float, total: float = 49_000.0) -> dict:
    return {
        "runtime": {
            "lanes": [],
            "devices": {
                "total_memory_mb": total * 2,
                "devices": [
                    {
                        "device_id": "GPU-0",
                        "extra": {"index": 0},
                        "memory_total_mb": total,
                        "memory_used_mb": max(total - free0, 0.0),
                        "memory_free_mb": free0,
                    },
                    {
                        "device_id": "GPU-1",
                        "extra": {"index": 1},
                        "memory_total_mb": total,
                        "memory_used_mb": max(total - free1, 0.0),
                        "memory_free_mb": free1,
                    },
                ],
            },
        }
    }


def _seed_demand(scores: Dict[str, float]) -> DemandTracker:
    tracker = DemandTracker()
    for name, score in scores.items():
        if score > 0:
            tracker._demand[name] = float(score)
            tracker._raw_count[name] = 1
            tracker._last_request[name] = 0.0
    return tracker


def _planner(
    provider: _MockProvider,
    scores: Dict[str, float],
    *,
    snapshot: Optional[dict] = None,
    demand: Optional[DemandTracker] = None,
) -> CapacityPlanner:
    facade = _MockFacade(provider)
    registry = MagicMock()
    registry.has_received_first_status.return_value = True
    registry.is_calibrating.return_value = False
    registry.is_provider_online.return_value = True
    registry.peek_runtime_snapshot.return_value = snapshot or _two_gpu_snapshot(free0=48_000.0, free1=48_000.0)

    planner = CapacityPlanner.__new__(CapacityPlanner)
    planner._facade = facade
    planner._registry = registry
    planner._demand = demand if demand is not None else _seed_demand(scores)
    planner._lane_wake_failure_until = {}
    planner._lane_load_failure_until = {}
    planner._lane_loaded_at = {}
    planner._lane_was_cold_loaded = {}
    planner._lane_idle_since = {}
    planner._lane_sleep_since = {}
    planner._lane_sleep_level = {}
    planner._drain_timestamps = {}
    planner._announced_use = {}
    planner._lane_waiters = {}
    planner._pending_capacity = {}
    planner._marked_cold_lanes = set()
    planner._lane_action_locks = {}
    planner._lane_lock_reader = None
    planner._eviction_gate_v2 = True
    planner._stop_dedup_siblings = False
    planner._cross_provider_dedup = False
    planner._cross_provider_best_first = False
    planner._replica_first_eviction = True
    planner._replicate_on_free_vram = False
    planner._provider_capacity_lock = lambda pid: SimpleNamespace(locked=lambda: False)
    planner._vram_ledger = SimpleNamespace(
        get_committed_mb=lambda pid: 0.0,
        has_overlapping_reservation=lambda *a, **k: False,
        get_gpu_effective_available_mb=lambda pid, g, f: f,
        cleanup_stale=lambda **k: 0,
    )
    planner._host_ram_ledger = SimpleNamespace(cleanup_stale=lambda **k: 0)

    # Accurate load params (TP / gpu placement) so _validate_vram_budget sees
    # real footprints — empty {} would mask per-model sizing.
    def _build_load_params(model_name, lane_id, profile, capacity, provider_id):
        tp = int(getattr(profile, "tensor_parallel_size", 1) or 1)
        return {
            "model": model_name,
            "tensor_parallel_size": tp,
            "gpu_devices": ",".join(str(i) for i in range(tp)) if tp > 1 else "0",
        }

    planner._build_load_params = _build_load_params
    planner._passes_minimum_load_feasibility = lambda *a, **k: True
    planner._load_requires_calibration = lambda *a, **k: False
    planner._safe_get_profiles = lambda pid: provider.profiles
    planner._safe_get_capacity = lambda pid: facade.get_capacity_info(pid)
    planner.get_pending_vram_mb = lambda pid: 0.0
    planner._enabled = False
    planner._tick_event = None
    planner._cycle_seconds = 10.0
    planner._unplannable_since = {}
    planner._unplannable_warned_at = {}
    planner._latency_store = None
    return planner


def _action_kinds(actions: List[CapacityPlanAction]) -> list[tuple[str, str]]:
    return [(a.action, a.model_name) for a in actions]


def _contention_scenario(
    *,
    finished_score: float,
    cold_score: float,
    cold_queue: int,
    now: float = 10_000.0,
    other_active_requests: int = 0,
):
    finished_sleeping = _lane(
        lane_id="lane-finished",
        model_name=FINISHED,
        runtime_state="sleeping",
        sleep_state="sleeping",
        effective_vram_mb=500.0,
        gpu_devices="0",
    )
    other_awake = _lane(
        lane_id="lane-other",
        model_name=OTHER,
        runtime_state="loaded",
        sleep_state="awake",
        effective_vram_mb=40_000.0,
        gpu_devices="1",
        active_requests=other_active_requests,
    )
    free0, free1 = 48_000.0, 8_000.0
    provider = _MockProvider(
        PROVIDER_ID,
        "W",
        lanes=[finished_sleeping, other_awake],
        profiles={
            FINISHED: _profile(40_000.0, sleeping_residual_mb=500.0),
            OTHER: _profile(40_000.0),
            COLD: _profile(80_000.0, tensor_parallel_size=2),
        },
        capabilities=[FINISHED, OTHER, COLD],
        available_vram_mb=free0 + free1,
        total_vram_mb=98_000.0,
        queue_depths={COLD: cold_queue, FINISHED: 0, OTHER: 0},
    )
    planner = _planner(
        provider,
        {FINISHED: finished_score, COLD: cold_score, OTHER: 0.1},
        snapshot=_two_gpu_snapshot(free0=free0, free1=free1),
    )
    planner._lane_loaded_at[(PROVIDER_ID, "lane-other")] = now - 60.0
    return planner, provider


class TestEffectiveDemandRanking:
    def test_queue1_outranks_higher_historical_queue0(self, monkeypatch):
        """score 0.58 queue0 → eff 0.58 vs score 0.34 queue1 → eff 0.84.

        TP2 cold needs BOTH GPUs; speculative wake of finished on GPU0 must not
        run alongside (or instead of) the queued cold load.
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(finished_score=0.58, cold_score=0.34, cold_queue=1, now=now)

        eff_finished = planner._effective_demand(FINISHED, PROVIDER_ID, provider.lanes)
        eff_cold = planner._effective_demand(COLD, PROVIDER_ID, provider.lanes)
        assert eff_cold == pytest.approx(0.34 + CapacityPlanner.QUEUE_WEIGHT * 1)
        assert eff_finished == pytest.approx(0.58)
        assert eff_cold > eff_finished

        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) in kinds, f"cold queued waiter must be planned; got {kinds}"
        assert (
            "wake",
            FINISHED,
        ) not in kinds, f"TP2 cold needs GPU0+GPU1; speculative wake must not consume GPU0; got {kinds}"
        validated = planner._validate_vram_budget(actions)
        assert ("wake", FINISHED) not in _action_kinds(validated)


class TestNoHealthySuccessCooldown:
    def test_confirmed_wake_does_not_arm_success_hold(self, monkeypatch):
        clock = {"now": 1_000.0}
        monkeypatch.setattr(planner_mod.time, "time", lambda: clock["now"])
        planner = _planner(_MockProvider(PROVIDER_ID, "W"), scores={})
        assert not hasattr(planner, "_load_cooldown_seconds")
        assert not hasattr(CapacityPlanner, "_lane_is_in_load_cooldown")
        assert not hasattr(CapacityPlanner, "_would_evict_cooled_lane")

        planner._record_confirmed_action_state(
            CapacityPlanAction(
                action="wake",
                provider_id=PROVIDER_ID,
                lane_id="lane-a",
                model_name=FINISHED,
                reason="test",
            ),
            confirmed_at=clock["now"],
        )
        # Tenure stamp remains for min-tenure / cold-waiter protection.
        assert planner._lane_loaded_at[(PROVIDER_ID, "lane-a")] == clock["now"]

    def test_irrelevant_gpu_no_longer_blocks_cold_load(self, monkeypatch):
        now = 5_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        cooled_irrelevant = _lane(
            lane_id="cooled-gpu0",
            model_name=OTHER,
            effective_vram_mb=40_000.0,
            gpu_devices="0",
        )
        victim_gpu1 = _lane(
            lane_id="victim-gpu1",
            model_name=FINISHED,
            effective_vram_mb=40_000.0,
            gpu_devices="1",
        )
        free0, free1 = 8_000.0, 8_000.0
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[cooled_irrelevant, victim_gpu1],
            profiles={
                COLD: _profile(40_000.0, tensor_parallel_size=1),
                OTHER: _profile(40_000.0),
                FINISHED: _profile(40_000.0),
            },
            capabilities=[COLD, OTHER, FINISHED],
            available_vram_mb=free0 + free1,
            queue_depths={COLD: 1},
        )
        planner = _planner(
            provider,
            scores={COLD: 2.0, OTHER: 5.0, FINISHED: 0.1},
            snapshot=_two_gpu_snapshot(free0=free0, free1=free1),
        )
        planner._lane_loaded_at[(PROVIDER_ID, "cooled-gpu0")] = now - 5.0
        planner._lane_loaded_at[(PROVIDER_ID, "victim-gpu1")] = now - 60.0

        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) in kinds, f"recent stamp on irrelevant GPU must not block; got {kinds}"


class TestFailureBackoffPreserved:
    def test_wake_failure_cooldown_still_blocks(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(finished_score=0.58, cold_score=0.0, cold_queue=0, now=now)
        planner._lane_wake_failure_until[(PROVIDER_ID, "lane-finished")] = now + 10.0
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("wake", FINISHED) not in _action_kinds(actions)

    def test_load_failure_cooldown_still_blocks(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[],
            profiles={COLD: _profile(20_000.0)},
            capabilities=[COLD],
            available_vram_mb=90_000.0,
            queue_depths={COLD: 1},
        )
        planner = _planner(provider, scores={COLD: 2.0}, snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0))
        lane_id = planner._planner_lane_id(COLD)
        planner._lane_load_failure_until[(PROVIDER_ID, lane_id)] = now + CapacityPlanner.LOAD_FAILURE_COOLDOWN_SECONDS
        blocked = planner._model_cold_load_blocked_reason(PROVIDER_ID, COLD, [])
        assert blocked is not None
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("load", COLD) not in _action_kinds(actions)


class TestSumVsMaxCompetitiveRatio:
    def test_two_victims_use_sum_not_max(self, monkeypatch):
        """Target must beat sum of two non-self victims, not only the max."""
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        # Two idle victims on GPUs 0 and 1; cold TP2 needs both.
        v0 = _lane(lane_id="v0", model_name=VICTIM_A, effective_vram_mb=40_000.0, gpu_devices="0")
        v1 = _lane(lane_id="v1", model_name=VICTIM_B, effective_vram_mb=40_000.0, gpu_devices="1")
        free0, free1 = 5_000.0, 5_000.0
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[v0, v1],
            profiles={
                COLD: _profile(80_000.0, tensor_parallel_size=2),
                VICTIM_A: _profile(40_000.0),
                VICTIM_B: _profile(40_000.0),
            },
            capabilities=[COLD, VICTIM_A, VICTIM_B],
            available_vram_mb=free0 + free1,
            queue_depths={},  # speculative load — ratio gate applies
        )
        # Each victim eff ≈ 0.6; sum=1.2; max=0.6. Target eff=1.0.
        # Under MAX: 1.0 > 0.6*2.0 = 1.2? No — wait, 1.0 > 1.2 is False.
        # Under SUM: 1.0 > 1.2*2.0 = 2.4? False.
        # Need a case where MAX would pass but SUM fails:
        # victims 0.4 and 0.4, ratio 2.0 → max gate needs >0.8, sum gate needs >1.6
        # target 1.0: passes max, fails sum.
        planner = _planner(
            provider,
            scores={COLD: 1.0, VICTIM_A: 0.4, VICTIM_B: 0.4},
            snapshot=_two_gpu_snapshot(free0=free0, free1=free1),
        )
        planner._lane_loaded_at[(PROVIDER_ID, "v0")] = now - 60.0
        planner._lane_loaded_at[(PROVIDER_ID, "v1")] = now - 60.0

        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) not in kinds, f"sum gate must reject target_eff=1.0 vs victim_sum=0.8×2.0; got {kinds}"

        # Raise target so sum gate passes: need > 0.8 * 2.0 = 1.6
        planner._demand._demand[COLD] = 1.7
        actions2 = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds2 = _action_kinds(actions2)
        assert ("load", COLD) in kinds2, f"sum gate must allow target_eff=1.7; got {kinds2}"


class TestFreeVramSpeculationVsQueuedWaiter:
    def test_busy_gpu1_blocks_speculative_wake_for_queued_tp2(self, monkeypatch):
        """Root repro: OTHER busy on GPU1; free GPU0; COLD TP2 queued; FINISHED
        sleeping queue0. Cold cannot place while GPU1 is busy, but speculative
        wake must not consume GPU0 the waiter still needs.
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(
            finished_score=0.58,
            cold_score=0.34,
            cold_queue=1,
            now=now,
            other_active_requests=1,
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("wake", FINISHED) not in kinds, f"speculative wake steals GPU0 from queued TP2; got {kinds}"
        assert ("load", COLD) not in kinds  # busy victim blocks placement
        validated = planner._validate_vram_budget(actions)
        assert ("wake", FINISHED) not in _action_kinds(validated)
        assert ("load", COLD) not in _action_kinds(validated)

    def test_planned_queued_tp2_claims_gpus_against_later_speculation(self, monkeypatch):
        """Once queued TP2 is planned (idle victim), speculative wake must not
        treat the pre-plan free snapshot as still available on those GPUs.
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(
            finished_score=0.58,
            cold_score=0.34,
            cold_queue=1,
            now=now,
            other_active_requests=0,
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) in kinds
        assert ("wake", FINISHED) not in kinds, f"planned TP2 claims both GPUs; got {kinds}"
        validated = planner._validate_vram_budget(actions)
        vkinds = _action_kinds(validated)
        assert ("load", COLD) in vkinds
        assert ("wake", FINISHED) not in vkinds


class TestDisjointSpeculativeCapacity:
    def test_free_vram_speculative_wake_still_allowed(self, monkeypatch):
        """Spare free VRAM: speculative wake proceeds when nothing queued waits."""
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        sleeping = _lane(
            lane_id="lane-finished",
            model_name=FINISHED,
            runtime_state="sleeping",
            sleep_state="sleeping",
            effective_vram_mb=500.0,
            gpu_devices="0",
        )
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[sleeping],
            profiles={FINISHED: _profile(40_000.0, sleeping_residual_mb=500.0)},
            capabilities=[FINISHED],
            available_vram_mb=90_000.0,
            queue_depths={},
        )
        planner = _planner(
            provider,
            scores={FINISHED: 0.58},
            snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0),
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("wake", FINISHED) in _action_kinds(actions)
        validated = planner._validate_vram_budget(actions)
        assert ("wake", FINISHED) in _action_kinds(validated)

    def test_disjoint_wake_while_queued_lane_on_other_gpu(self, monkeypatch):
        """Queued waiter already has a sleeping lane on GPU1 only; speculative
        free-VRAM wake on GPU0 is genuinely disjoint and may proceed.
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        finished = _lane(
            lane_id="lane-finished",
            model_name=FINISHED,
            runtime_state="sleeping",
            sleep_state="sleeping",
            effective_vram_mb=500.0,
            gpu_devices="0",
        )
        queued_sleeping = _lane(
            lane_id="lane-cold",
            model_name=COLD,
            runtime_state="sleeping",
            sleep_state="sleeping",
            effective_vram_mb=500.0,
            gpu_devices="1",
        )
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[finished, queued_sleeping],
            profiles={
                FINISHED: _profile(20_000.0, sleeping_residual_mb=500.0),
                COLD: _profile(20_000.0, sleeping_residual_mb=500.0, tensor_parallel_size=1),
            },
            capabilities=[FINISHED, COLD],
            available_vram_mb=90_000.0,
            queue_depths={COLD: 1, FINISHED: 0},
        )
        # Free enough on both GPUs that neither wake needs eviction.
        planner = _planner(
            provider,
            scores={FINISHED: 0.58, COLD: 0.34},
            snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0),
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("wake", COLD) in kinds, f"queued existing lane must wake; got {kinds}"
        assert ("wake", FINISHED) in kinds, f"disjoint GPU0 speculative wake should proceed; got {kinds}"
        validated = planner._validate_vram_budget(actions)
        vkinds = _action_kinds(validated)
        assert ("wake", COLD) in vkinds
        assert ("wake", FINISHED) in vkinds


class TestActiveQueuedVictimsProtected:
    def test_busy_victim_not_evicted(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        busy = _lane(
            lane_id="busy",
            model_name=OTHER,
            effective_vram_mb=40_000.0,
            gpu_devices="0",
            active_requests=2,
        )
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[busy],
            profiles={
                COLD: _profile(40_000.0),
                OTHER: _profile(40_000.0),
            },
            capabilities=[COLD, OTHER],
            available_vram_mb=5_000.0,
            queue_depths={COLD: 1},
        )
        planner = _planner(
            provider,
            scores={COLD: 2.0, OTHER: 0.1},
            snapshot=_two_gpu_snapshot(free0=5_000.0, free1=48_000.0),
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert not any(a == "sleep_l1" and m == OTHER for a, m in kinds)
        assert not any(a == "stop" and m == OTHER for a, m in kinds)

    def test_queued_victim_model_not_sacrificed_under_ratio(self, monkeypatch):
        """Victim with real queue and above-floor demand is not idle-bypassable."""
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        # Victim idle at vLLM but has scheduler queue → under v2 can be eviction
        # candidate, but victim_below_floor is false when eff >= floor, so
        # queued cold must beat sum*ratio.
        victim = _lane(
            lane_id="vic",
            model_name=OTHER,
            effective_vram_mb=40_000.0,
            gpu_devices="0",
            active_requests=0,
            queue_waiting=0.0,
        )
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[victim],
            profiles={COLD: _profile(40_000.0), OTHER: _profile(40_000.0)},
            capabilities=[COLD, OTHER],
            available_vram_mb=5_000.0,
            queue_depths={COLD: 1, OTHER: 3},
        )
        # OTHER eff = 1.0 + 0.5*3 = 2.5; COLD eff = 0.5 + 0.5 = 1.0
        # bypass requires victim_below_floor; 2.5 >= LOAD floor → ratio needed
        # 1.0 > 2.5 * 2.0? No → must not load by displacing OTHER.
        planner = _planner(
            provider,
            scores={COLD: 0.5, OTHER: 1.0},
            snapshot=_two_gpu_snapshot(free0=5_000.0, free1=48_000.0),
        )
        planner._lane_loaded_at[(PROVIDER_ID, "vic")] = now - 60.0
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) not in kinds or not any(
            a in ("sleep_l1", "stop") and m == OTHER for a, m in kinds
        ), f"queued victim must not be displaced by weaker cold; got {kinds}"


class TestReplicationAndSeedingGuards:
    def test_replication_blocked_when_queued_tp2_needs_gpus(self, monkeypatch):
        """Default replicate-on-free-vram: historic replica must not consume GPUs
        a same-provider queued TP2 waiter still needs (busy GPU1 blocks cold).
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(
            finished_score=0.0,
            cold_score=0.34,
            cold_queue=1,
            now=now,
            other_active_requests=1,
        )
        planner._replicate_on_free_vram = True
        planner._is_plannable = lambda pid: True
        # Demand pass: cold cannot place; no finished wake (score 0).
        demand = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        claimed: dict[int, set[int]] = {}
        # Re-run demand with shared claimed bookkeeping (as _run_cycle does).
        demand = planner._compute_demand_actions(
            PROVIDER_ID,
            provider.lanes,
            cycle_claimed_gpus_by_provider=claimed,
        )
        assert ("load", COLD) not in _action_kinds(demand)
        # Historic hot model already served elsewhere → replication candidate.
        repl = planner._compute_replication_actions(
            [PROVIDER_ID],
            [(FINISHED, CapacityPlanner.DEMAND_REPLICATION_FLOOR + 0.5)],
            {FINISHED: 1},
            set(),
            set(),
            {PROVIDER_ID: set()},
            claimed,
        )
        kinds = _action_kinds(repl)
        assert ("load", FINISHED) not in kinds, f"replica must not steal GPUs from queued TP2; got {kinds}"
        validated = planner._validate_vram_budget(demand + repl)
        assert ("load", FINISHED) not in _action_kinds(validated)

    def test_replication_allowed_when_no_queued_waiter(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[],
            profiles={FINISHED: _profile(20_000.0)},
            capabilities=[FINISHED],
            available_vram_mb=90_000.0,
            queue_depths={},
        )
        planner = _planner(
            provider,
            scores={FINISHED: CapacityPlanner.DEMAND_REPLICATION_FLOOR + 1.0},
            snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0),
        )
        planner._replicate_on_free_vram = True
        planner._is_plannable = lambda pid: True
        claimed: dict[int, set[int]] = {}
        repl = planner._compute_replication_actions(
            [PROVIDER_ID],
            [(FINISHED, CapacityPlanner.DEMAND_REPLICATION_FLOOR + 1.0)],
            {FINISHED: 1},
            set(),
            set(),
            {PROVIDER_ID: set()},
            claimed,
        )
        assert ("load", FINISHED) in _action_kinds(repl)
        assert ("load", FINISHED) in _action_kinds(planner._validate_vram_budget(repl))

    def test_capability_seed_queued_first_lane_not_skipped(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[],
            profiles={COLD: _profile(20_000.0)},
            capabilities=[COLD],
            available_vram_mb=90_000.0,
            queue_depths={COLD: 1},
        )
        planner = _planner(
            provider,
            scores={COLD: 0.1},
            snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0),
        )
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("load", COLD) in _action_kinds(actions)
        assert ("load", COLD) in _action_kinds(planner._validate_vram_budget(actions))

    def test_capability_seed_honors_load_failure_backoff(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[],
            profiles={COLD: _profile(20_000.0)},
            capabilities=[COLD],
            available_vram_mb=90_000.0,
            queue_depths={COLD: 1},
        )
        planner = _planner(
            provider,
            scores={COLD: 2.0},
            snapshot=_two_gpu_snapshot(free0=48_000.0, free1=48_000.0),
        )
        lane_id = planner._planner_lane_id(COLD)
        planner._lane_load_failure_until[(PROVIDER_ID, lane_id)] = now + CapacityPlanner.LOAD_FAILURE_COOLDOWN_SECONDS
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("load", COLD) not in _action_kinds(actions)

    def test_same_provider_additional_lane_yields_to_queued_tp2(self, monkeypatch):
        """Additional free-VRAM copy of a hot awake model must not take GPU0
        while queued TP2 still needs both GPUs (GPU1 busy).
        """
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        hot = _lane(
            lane_id="lane-hot",
            model_name=FINISHED,
            runtime_state="loaded",
            sleep_state="awake",
            effective_vram_mb=5_000.0,
            gpu_devices="0",
        )
        busy = _lane(
            lane_id="lane-other",
            model_name=OTHER,
            runtime_state="loaded",
            sleep_state="awake",
            effective_vram_mb=40_000.0,
            gpu_devices="1",
            active_requests=1,
        )
        free0, free1 = 40_000.0, 8_000.0
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=[hot, busy],
            profiles={
                FINISHED: _profile(5_000.0),
                OTHER: _profile(40_000.0),
                COLD: _profile(80_000.0, tensor_parallel_size=2),
            },
            capabilities=[FINISHED, OTHER, COLD],
            available_vram_mb=free0 + free1,
            queue_depths={COLD: 1, FINISHED: 2},
        )
        planner = _planner(
            provider,
            scores={FINISHED: CapacityPlanner.DEMAND_REPLICATION_FLOOR + 1.0, COLD: 0.34, OTHER: 0.1},
            snapshot=_two_gpu_snapshot(free0=free0, free1=free1),
        )
        planner._replicate_on_free_vram = True
        planner._lane_loaded_at[(PROVIDER_ID, "lane-hot")] = now - 60.0
        planner._lane_loaded_at[(PROVIDER_ID, "lane-other")] = now - 60.0
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        # Additional FINISHED lane is speculative scale-out even with queue
        # (awake lane already serves); must not plan it over blocked TP2 needs.
        assert not any(a == "load" and m == FINISHED for a, m in kinds), f"got {kinds}"
        validated = planner._validate_vram_budget(actions)
        assert not any(a == "load" and m == FINISHED for a, m in _action_kinds(validated))


class TestSumThreeVictimsOptional:
    def test_three_small_victims_sum_gate(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        victims = [
            _lane(lane_id=f"v{i}", model_name=f"org/v{i}", effective_vram_mb=25_000.0, gpu_devices=str(i % 2))
            for i in range(3)
        ]
        # Force TP1 placement that needs eviction of multiple idle victims:
        # use aggregate-tight free so pick_cold_load needs victims.
        provider = _MockProvider(
            PROVIDER_ID,
            "W",
            lanes=victims,
            profiles={
                COLD: _profile(40_000.0, tensor_parallel_size=1),
                **{f"org/v{i}": _profile(25_000.0) for i in range(3)},
            },
            capabilities=[COLD] + [f"org/v{i}" for i in range(3)],
            available_vram_mb=10_000.0,
            queue_depths={},
        )
        # Three victims at 0.3 each → sum=0.9; max=0.3. Target 0.7:
        # max gate 0.7 > 0.3*2=0.6 would pass; sum gate 0.7 > 0.9*2=1.8 fails.
        scores = {COLD: 0.7, **{f"org/v{i}": 0.3 for i in range(3)}}
        planner = _planner(
            provider,
            scores=scores,
            snapshot=_two_gpu_snapshot(free0=5_000.0, free1=5_000.0),
        )
        for i in range(3):
            planner._lane_loaded_at[(PROVIDER_ID, f"v{i}")] = now - 60.0
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        assert ("load", COLD) not in _action_kinds(actions)
