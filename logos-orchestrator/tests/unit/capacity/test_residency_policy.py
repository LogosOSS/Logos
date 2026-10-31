"""Residency policy switch (LOGOS_RESIDENCY_POLICY).

"demand" (default) orders eviction victims by effective demand; "lru" orders
them by idle time (least recently used first) and disables the demand-ratio
gates on speculative wake/load. Sleep-before-stop, drain, tenure, backoff, and
queued-residency GPU safeguards are shared.
"""

from __future__ import annotations

import time

import pytest

from logos import CapacityPlanner
from logos.capacity import capacity_planner as planner_mod

from .test_best_first_scenarios import _lane, _MockProvider, _planner, _profile
from .test_queued_residency_fairness import (
    COLD,
    FINISHED,
    PROVIDER_ID,
    VICTIM_A,
    VICTIM_B,
    _action_kinds,
    _contention_scenario,
)
from .test_queued_residency_fairness import _lane as _fairness_lane
from .test_queued_residency_fairness import _MockProvider as _FairnessProvider
from .test_queued_residency_fairness import _planner as _fairness_planner
from .test_queued_residency_fairness import _profile as _fairness_profile
from .test_queued_residency_fairness import _two_gpu_snapshot


def _two_idle_lanes_provider() -> _MockProvider:
    """Two awake, idle lanes on GPU 0; evicting either one covers the deficit."""
    return _MockProvider(
        provider_id=1,
        name="A",
        lanes=[
            _lane(lane_id="A-hot", model_name="Hot", effective_vram_mb=20_000, gpu_devices="0"),
            _lane(lane_id="A-cold", model_name="Cold", effective_vram_mb=20_000, gpu_devices="0"),
        ],
        profiles={
            "Hot": _profile(loaded_vram_mb=20_000, sleeping_residual_mb=500.0),
            "Cold": _profile(loaded_vram_mb=20_000, sleeping_residual_mb=500.0),
        },
    )


def _evict_one(planner: CapacityPlanner, provider: _MockProvider) -> str:
    eviction = planner._find_eviction_set(
        provider_id=1,
        required_gpus=frozenset({0}),
        per_gpu_deficit={0: 5_000.0},
        lanes=provider.lanes,
        profiles=provider.profiles,
    )
    assert eviction is not None and len(eviction) == 1
    lane, action, _eff = eviction[0]
    assert action == "sleep_l1"
    return lane.model_name


def _setup(policy: str | None) -> tuple[CapacityPlanner, _MockProvider]:
    provider = _two_idle_lanes_provider()
    planner = _planner([provider])
    if policy is not None:
        planner._residency_policy = policy
    now = time.time()
    # "Hot" was idle for a long time (least recently used) but carries high
    # demand; "Cold" was used a moment ago but has no demand. The two policies
    # must therefore pick different victims.
    planner._lane_idle_since = {
        planner._lane_key(1, "A-hot"): now - 600.0,
        planner._lane_key(1, "A-cold"): now - 5.0,
    }
    planner._demand.get_score = lambda model, *_a, **_k: 5.0 if model == "Hot" else 0.0
    return planner, provider


class TestParseResidencyPolicy:
    @pytest.mark.parametrize("raw", [None, "", "demand", "DEMAND", " demand "])
    def test_defaults_to_demand(self, raw):
        assert CapacityPlanner._parse_residency_policy(raw) == "demand"

    @pytest.mark.parametrize("raw", ["lru", "LRU", " lru "])
    def test_accepts_lru(self, raw):
        assert CapacityPlanner._parse_residency_policy(raw) == "lru"

    def test_unknown_value_falls_back_to_demand(self):
        assert CapacityPlanner._parse_residency_policy("fifo") == "demand"


class TestEvictionOrder:
    def test_planner_without_attribute_uses_demand_policy(self):
        planner, _ = _setup(policy=None)
        assert planner.residency_policy == "demand"

    def test_demand_policy_evicts_lowest_demand(self):
        planner, provider = _setup(policy="demand")
        assert _evict_one(planner, provider) == "Cold"

    def test_lru_policy_evicts_least_recently_used(self):
        planner, provider = _setup(policy="lru")
        assert _evict_one(planner, provider) == "Hot"

    def test_lru_policy_still_prefers_sleep_over_stop(self):
        """A lane that can only be stopped must not be chosen ahead of a
        sleepable one, even if it has been idle longer."""
        planner, provider = _setup(policy="lru")
        # Make the long-idle lane unsleepable: a model without a sleeping
        # residual cannot be slept, so its only eviction action is "stop".
        provider.profiles["Hot"] = _profile(loaded_vram_mb=20_000, sleeping_residual_mb=20_000.0)
        eviction = planner._find_eviction_set(
            provider_id=1,
            required_gpus=frozenset({0}),
            per_gpu_deficit={0: 5_000.0},
            lanes=provider.lanes,
            profiles=provider.profiles,
        )
        assert eviction is not None
        first_lane, first_action, _ = eviction[0]
        assert first_action == "sleep_l1"
        assert first_lane.model_name == "Cold"


class TestLruRatioBypass:
    """Speculative load that fails the demand sum-ratio under ``demand`` must
    still proceed under ``lru`` — selection is exercised via ``_compute_demand_actions``,
    not log scraping.
    """

    def _sum_gate_provider(self) -> _FairnessProvider:
        v0 = _fairness_lane(lane_id="v0", model_name=VICTIM_A, effective_vram_mb=40_000.0, gpu_devices="0")
        v1 = _fairness_lane(lane_id="v1", model_name=VICTIM_B, effective_vram_mb=40_000.0, gpu_devices="1")
        return _FairnessProvider(
            PROVIDER_ID,
            "W",
            lanes=[v0, v1],
            profiles={
                COLD: _fairness_profile(80_000.0, tensor_parallel_size=2),
                VICTIM_A: _fairness_profile(40_000.0),
                VICTIM_B: _fairness_profile(40_000.0),
            },
            capabilities=[COLD, VICTIM_A, VICTIM_B],
            available_vram_mb=10_000.0,
            queue_depths={},  # speculative — ratio gate applies under demand
        )

    def test_demand_policy_rejects_sub_ratio_speculative_load(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = self._sum_gate_provider()
        # victims 0.4+0.4=0.8; LOAD_COMPETITIVE_RATIO=2.0 → need >1.6; target 1.0 fails
        planner = _fairness_planner(
            provider,
            scores={COLD: 1.0, VICTIM_A: 0.4, VICTIM_B: 0.4},
            snapshot=_two_gpu_snapshot(free0=5_000.0, free1=5_000.0),
        )
        planner._residency_policy = "demand"
        planner._lane_loaded_at[(PROVIDER_ID, "v0")] = now - 60.0
        planner._lane_loaded_at[(PROVIDER_ID, "v1")] = now - 60.0
        kinds = _action_kinds(planner._compute_demand_actions(PROVIDER_ID, provider.lanes))
        assert ("load", COLD) not in kinds

    def test_lru_policy_bypasses_ratio_and_loads(self, monkeypatch):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        provider = self._sum_gate_provider()
        planner = _fairness_planner(
            provider,
            scores={COLD: 1.0, VICTIM_A: 0.4, VICTIM_B: 0.4},
            snapshot=_two_gpu_snapshot(free0=5_000.0, free1=5_000.0),
        )
        planner._residency_policy = "lru"
        planner._lane_loaded_at[(PROVIDER_ID, "v0")] = now - 60.0
        planner._lane_loaded_at[(PROVIDER_ID, "v1")] = now - 60.0
        # Idle ages differ so LRU victim order is deterministic if eviction runs.
        planner._lane_idle_since[(PROVIDER_ID, "v0")] = now - 600.0
        planner._lane_idle_since[(PROVIDER_ID, "v1")] = now - 5.0
        actions = planner._compute_demand_actions(PROVIDER_ID, provider.lanes)
        kinds = _action_kinds(actions)
        assert ("load", COLD) in kinds, f"lru must bypass sum-ratio gate; got {kinds}"
        # Eviction must accompany the load when free VRAM is insufficient.
        assert any(a.action in {"sleep_l1", "sleep_l2", "stop"} for a in actions)


class TestSharedPolicySafety:
    @pytest.mark.parametrize("policy", ["demand", "lru"])
    @pytest.mark.parametrize("active_requests", [0, 1])
    def test_queued_tp2_keeps_capacity_from_speculative_wake(self, monkeypatch, policy, active_requests):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(
            finished_score=0.58, cold_score=0.34, cold_queue=1, now=now, other_active_requests=active_requests
        )
        planner._residency_policy = policy
        actions = planner._validate_vram_budget(planner._compute_demand_actions(PROVIDER_ID, provider.lanes))
        kinds = _action_kinds(actions)
        assert ("wake", FINISHED) not in kinds
        assert (("load", COLD) in kinds) == (active_requests == 0)

    @pytest.mark.parametrize("policy", ["demand", "lru"])
    def test_failed_wake_backoff_remains_active(self, monkeypatch, policy):
        now = 10_000.0
        monkeypatch.setattr(planner_mod.time, "time", lambda: now)
        planner, provider = _contention_scenario(finished_score=0.58, cold_score=0.0, cold_queue=0, now=now)
        planner._residency_policy = policy
        planner._lane_wake_failure_until[(PROVIDER_ID, "lane-finished")] = now + 10.0
        assert ("wake", FINISHED) not in _action_kinds(planner._compute_demand_actions(PROVIDER_ID, provider.lanes))
