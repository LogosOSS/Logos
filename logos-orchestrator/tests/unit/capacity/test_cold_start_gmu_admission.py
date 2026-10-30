"""Cold-start VRAM admission: startup GMU gate + per-GPU accounting.

vLLM's EngineCore init refuses free < gmu×total even when kv_cache_memory_bytes
pins the steady-state KV pool. Admission must check the exact requested GPU
set (when pinned), debit accepted load/wake footprints across the batch, never
invent free VRAM for victims without known placement, filter mixed-size cards
by each card's own need, and read calculated startup GMU from the worker
(configured / derived / default 0.9) rather than inventing the auto-derive floor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from logos.capacity.capacity_planner import CapacityPlanAction, CapacityPlanner
from logos.capacity.vram_ledger import VRAMLedger
from logos.sdi.models import ModelProfile

PROVIDER_ID = 1
MODEL = "google/gemma-3-4b-it"
MODEL_B = "org/other-model"

GPU_TOTAL_MB = 47.37 * 1024
FREE_BELOW_GMU_MB = 21.04 * 1024
FOOTPRINT_MB = 18824.0
PINNED_GMU = 0.5
GMU_NEED_MB = PINNED_GMU * GPU_TOTAL_MB


def _profile(
    *,
    model_name: str = MODEL,
    loaded_mb: float = FOOTPRINT_MB,
    observed_gmu: float | None = PINNED_GMU,
    tp: int = 1,
    residual_mb: float = 1000.0,
) -> ModelProfile:
    return ModelProfile(
        model_name=model_name,
        engine="vllm",
        residency_source="calibrated",
        base_residency_mb=41_000.0,
        loaded_vram_mb=loaded_mb,
        sleeping_residual_mb=residual_mb,
        tensor_parallel_size=tp,
        observed_gpu_memory_utilization=observed_gmu,
        min_gpu_memory_utilization_to_load=observed_gmu,
        kv_budget_mb=13.0 * 1024,
    )


def _capacity(*, available_mb: float, total_mb: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        available_vram_mb=available_mb,
        total_vram_mb=total_mb if total_mb is not None else 2 * GPU_TOTAL_MB,
    )


def _planner(
    *,
    free: dict[int, float],
    total: dict[int, float] | None = None,
    profiles: dict[str, ModelProfile] | None = None,
    available_mb: float | None = None,
    configured_vllm: dict | None = None,
    startup_gmu: dict | None = None,
    include_configured_field: bool = True,
    include_startup_gmu_field: bool = False,
    raw_free_before_ledger: dict[int, float] | None = None,
    ledger: VRAMLedger | None = None,
) -> CapacityPlanner:
    totals = total or {gpu: GPU_TOTAL_MB for gpu in free}
    profiles = profiles or {MODEL: _profile()}
    planner = CapacityPlanner(logosnode_facade=None, logosnode_registry=None, demand_tracker=None)
    facade = MagicMock()
    facade.get_capacity_info.return_value = _capacity(
        available_mb=available_mb if available_mb is not None else sum(free.values())
    )
    facade.get_model_profiles.return_value = profiles
    facade.get_provider_name.side_effect = lambda pid: f"worker-{pid}"
    planner._facade = facade
    planner.get_pending_vram_mb = lambda pid: 0.0  # noqa: E731
    if ledger is not None:
        planner._vram_ledger = ledger
        # Mimic production _get_per_gpu_free: apply ledger once.
        source = raw_free_before_ledger if raw_free_before_ledger is not None else free

        def _free(pid: int) -> dict[int, float]:
            return {gpu: ledger.get_gpu_effective_available_mb(pid, gpu, float(mb)) for gpu, mb in source.items()}

        planner._get_per_gpu_free = _free  # noqa: E731
    else:
        planner._get_per_gpu_free = lambda pid: dict(free)  # noqa: E731
        planner._vram_ledger = SimpleNamespace(
            get_gpu_effective_available_mb=lambda pid, g, f: f,
            get_committed_mb=lambda pid: 0.0,
        )
    planner._get_per_gpu_total = lambda pid: dict(totals)  # noqa: E731
    runtime: dict = {"lanes": []}
    if include_configured_field:
        runtime["configured_vllm_by_model"] = configured_vllm if configured_vllm is not None else {}
    if include_startup_gmu_field or startup_gmu is not None:
        runtime["startup_gmu_by_model"] = startup_gmu if startup_gmu is not None else {}
    registry = MagicMock()
    registry.peek_runtime_snapshot.return_value = {"runtime": runtime}
    planner._registry = registry
    planner._lane_gpu_devices_str = lambda pid, lid: None  # noqa: E731
    return planner


def _load(*, lane_id: str = "planner-a", model: str = MODEL, gpu_devices: str | None = None) -> CapacityPlanAction:
    params = {"lane_id": lane_id, "model": model}
    if gpu_devices is not None:
        params["gpu_devices"] = gpu_devices
    return CapacityPlanAction(
        action="load",
        provider_id=PROVIDER_ID,
        lane_id=lane_id,
        model_name=model,
        params=params,
    )


def _wake(*, lane_id: str = "planner-a", model: str = MODEL, gpu_devices: str | None = None) -> CapacityPlanAction:
    params = {}
    if gpu_devices is not None:
        params["gpu_devices"] = gpu_devices
    return CapacityPlanAction(
        action="wake",
        provider_id=PROVIDER_ID,
        lane_id=lane_id,
        model_name=model,
        params=params or None,
    )


# ---------------------------------------------------------------------------
# Double ledger subtraction
# ---------------------------------------------------------------------------


def test_ledger_applied_once_raw40_pending10_effective30_admits_need24():
    """_get_per_gpu_free already ledger-adjusts; validate must not subtract again."""
    raw = 40.0 * 1024
    pending = 10.0 * 1024
    total = 48.0 * 1024
    ledger = VRAMLedger()
    # Reserve 10 GiB against GPU 0 (in-flight).
    ledger.reserve(
        provider_id=PROVIDER_ID,
        lane_id="inflight-lane",
        operation="load",
        vram_mb=pending,
        gpu_devices="0",
    )
    planner = _planner(
        free={0: raw - pending},  # unused when ledger path set
        raw_free_before_ledger={0: raw},
        total={0: total},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=8_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
        available_mb=raw - pending,
        ledger=ledger,
    )
    # Effective free = 30 GiB; cold need = 0.5 × 48 = 24 GiB → admit.
    free_map = planner._get_per_gpu_free(PROVIDER_ID)  # noqa: SLF001
    assert free_map[0] == pytest.approx(30.0 * 1024, rel=1e-6)
    validated = planner._validate_vram_budget([_load(gpu_devices="0")])  # noqa: SLF001
    assert len(validated) == 1


# ---------------------------------------------------------------------------
# A — pinned target GPU set
# ---------------------------------------------------------------------------


def test_a_pinned_gpu0_rejected_even_when_gpu1_has_room():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB, 1: 45.0 * 1024},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_a_pinned_wrong_count_vs_tp_rejected():
    profile = _profile(tp=2, loaded_mb=40_000.0, observed_gmu=None)
    planner = _planner(
        free={0: 40_000.0, 1: 40_000.0},
        profiles={MODEL: profile},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_a_unspecified_placement_selects_feasible_gpu_and_records_it():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB, 1: 45.0 * 1024},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    action = _load()
    validated = planner._validate_vram_budget([action])  # noqa: SLF001
    assert len(validated) == 1
    assert validated[0].params["gpu_devices"] == "1"


# ---------------------------------------------------------------------------
# B — cumulative per-GPU debits
# ---------------------------------------------------------------------------


def test_b_two_loads_same_gpu_second_rejected_after_debit():
    free = 35.0 * 1024
    total = 48.0 * 1024
    planner = _planner(
        free={0: free, 1: free},
        total={0: total, 1: total},
        profiles={
            MODEL: _profile(observed_gmu=None, loaded_mb=10_000.0),
            MODEL_B: _profile(model_name=MODEL_B, observed_gmu=None, loaded_mb=10_000.0),
        },
        startup_gmu={
            MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"},
            MODEL_B: {"gpu_memory_utilization": 0.5, "source": "configured"},
        },
        available_mb=2 * free,
    )
    a1 = _load(lane_id="lane-1", model=MODEL, gpu_devices="0")
    a2 = _load(lane_id="lane-2", model=MODEL_B, gpu_devices="0")
    validated = planner._validate_vram_budget([a1, a2])  # noqa: SLF001
    assert [a.lane_id for a in validated] == ["lane-1"]


def test_b_wake_debits_footprint_without_gmu_gate():
    total = 48.0 * 1024
    free = 22.0 * 1024
    planner = _planner(
        free={0: free, 1: 40.0 * 1024},
        total={0: total, 1: total},
        profiles={
            MODEL: _profile(observed_gmu=None, loaded_mb=20_000.0, residual_mb=16_000.0),
            MODEL_B: _profile(model_name=MODEL_B, observed_gmu=None, loaded_mb=8_000.0),
        },
        startup_gmu={
            MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"},
            MODEL_B: {"gpu_memory_utilization": 0.5, "source": "configured"},
        },
        available_mb=free + 40.0 * 1024,
    )
    wake = _wake(lane_id="wake-1", gpu_devices="0")
    load = _load(lane_id="load-1", model=MODEL_B, gpu_devices="0")
    validated = planner._validate_vram_budget([wake, load])  # noqa: SLF001
    assert [a.action for a in validated] == ["wake"]


# ---------------------------------------------------------------------------
# C — unknown victim placement
# ---------------------------------------------------------------------------


def test_c_unknown_victim_placement_does_not_credit_gpus():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB, 1: FREE_BELOW_GMU_MB},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    stop = CapacityPlanAction(
        action="stop",
        provider_id=PROVIDER_ID,
        lane_id="victim",
        model_name=MODEL_B,
    )
    planner._facade.get_model_profiles.return_value = {
        MODEL: _profile(),
        MODEL_B: _profile(model_name=MODEL_B, loaded_mb=40_000.0),
    }
    planner._lane_gpu_devices_str = lambda pid, lid: None  # noqa: E731
    validated = planner._validate_vram_budget([stop, _load(gpu_devices="0")])  # noqa: SLF001
    assert [a.action for a in validated] == ["stop"]


def test_c_known_victim_placement_credits_only_those_gpus():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB, 1: FREE_BELOW_GMU_MB},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    stop = CapacityPlanAction(
        action="stop",
        provider_id=PROVIDER_ID,
        lane_id="victim",
        model_name=MODEL_B,
    )
    planner._facade.get_model_profiles.return_value = {
        MODEL: _profile(),
        MODEL_B: _profile(model_name=MODEL_B, loaded_mb=20_000.0),
    }
    planner._lane_gpu_devices_str = lambda pid, lid: "0" if lid == "victim" else None  # noqa: E731
    validated = planner._validate_vram_budget([stop, _load(gpu_devices="0")])  # noqa: SLF001
    assert {a.action for a in validated} == {"stop", "load"}


# ---------------------------------------------------------------------------
# D — mixed cards
# ---------------------------------------------------------------------------


def test_d_mixed_cards_selects_smaller_feasible_gpu():
    planner = _planner(
        free={0: 30_000.0, 1: 25_000.0},
        total={0: 80_000.0, 1: 40_000.0},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=5_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
        available_mb=55_000.0,
    )
    action = _load()
    validated = planner._validate_vram_budget([action])  # noqa: SLF001
    assert len(validated) == 1
    assert validated[0].params["gpu_devices"] == "1"


# ---------------------------------------------------------------------------
# E — authoritative startup GMU (configured / derived / default)
# ---------------------------------------------------------------------------


def test_e_configured_gmu_0_83_rejects_when_profile_fields_none():
    total = 48.0 * 1024
    free = 30.0 * 1024
    planner = _planner(
        free={0: free, 1: free},
        total={0: total, 1: total},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=10_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.83, "source": "configured"}},
        available_mb=2 * free,
    )
    assert planner._startup_gmu_for_model(PROVIDER_ID, MODEL, _profile(observed_gmu=None)) == 0.83  # noqa: SLF001
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_e_empty_configured_map_does_not_imply_floor_half():
    """Empty configured_vllm_by_model must not invent GMU 0.5."""
    total = 48.0 * 1024
    free = 30.0 * 1024  # clears 0.5×48 but not default 0.9×48
    planner = _planner(
        free={0: free},
        total={0: total},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=8_000.0)},
        configured_vllm={},
        include_startup_gmu_field=False,
        available_mb=free,
    )
    # No startup_gmu publish, no profile pin → vLLM default 0.9.
    assert planner._startup_gmu_for_model(PROVIDER_ID, MODEL, _profile(observed_gmu=None)) == 0.9  # noqa: SLF001
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_e_published_default_source_uses_vllm_0_9():
    total = 48.0 * 1024
    free = 35.0 * 1024  # below 0.9×48≈43.2
    planner = _planner(
        free={0: free},
        total={0: total},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=8_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.9, "source": "default"}},
        available_mb=free,
    )
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_e_published_derived_source_used():
    total = 48.0 * 1024
    free = 30.0 * 1024
    derived = 0.72
    planner = _planner(
        free={0: free},
        total={0: total},
        profiles={MODEL: _profile(observed_gmu=None, loaded_mb=8_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": derived, "source": "derived"}},
        available_mb=free,
    )
    assert planner._startup_gmu_for_model(PROVIDER_ID, MODEL, _profile(observed_gmu=None)) == derived  # noqa: SLF001
    # 0.72 × 48 GiB ≈ 34.56 GiB > 30 → reject
    assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_e_startup_gmu_overrides_stale_profile_observed():
    """Late profile observed must not beat worker-published startup GMU."""
    planner = _planner(
        free={0: 30.0 * 1024},
        total={0: 48.0 * 1024},
        profiles={MODEL: _profile(observed_gmu=0.5, loaded_mb=8_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.83, "source": "configured"}},
        available_mb=30.0 * 1024,
    )
    assert planner._startup_gmu_for_model(PROVIDER_ID, MODEL, _profile(observed_gmu=0.5)) == 0.83  # noqa: SLF001


# ---------------------------------------------------------------------------
# Positive controls
# ---------------------------------------------------------------------------


def test_homogeneous_free_below_gmu_still_rejected():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB, 1: FREE_BELOW_GMU_MB},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    assert planner._validate_vram_budget([_load()]) == []  # noqa: SLF001


def test_homogeneous_free_above_gmu_admitted():
    planner = _planner(
        free={0: GMU_NEED_MB + 1024.0, 1: GMU_NEED_MB + 1024.0},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
    )
    assert len(planner._validate_vram_budget([_load()])) == 1  # noqa: SLF001


def test_lru_and_demand_both_apply_pinned_gpu_gate():
    for policy in ("demand", "lru"):
        planner = _planner(
            free={0: FREE_BELOW_GMU_MB, 1: 45.0 * 1024},
            startup_gmu={MODEL: {"gpu_memory_utilization": 0.5, "source": "configured"}},
        )
        planner._residency_policy = planner._parse_residency_policy(policy)  # noqa: SLF001
        assert planner.residency_policy == policy
        assert planner._validate_vram_budget([_load(gpu_devices="0")]) == []  # noqa: SLF001


def test_wake_without_gmu_when_footprint_fits():
    planner = _planner(
        free={0: FREE_BELOW_GMU_MB},
        profiles={MODEL: _profile(loaded_mb=FOOTPRINT_MB, residual_mb=FOOTPRINT_MB - 3_000.0)},
        startup_gmu={MODEL: {"gpu_memory_utilization": 0.9, "source": "default"}},
    )
    validated = planner._validate_vram_budget([_wake(gpu_devices="0")])  # noqa: SLF001
    assert [a.action for a in validated] == ["wake"]
