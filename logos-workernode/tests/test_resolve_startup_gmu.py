"""Shared resolve_startup_gmu branches: configured / derived / default."""

from __future__ import annotations

import pytest

from logos_worker_node.model_profiles import ModelProfileRecord
from logos_worker_node.vllm_process import GMU_AUTO_CEILING, GMU_AUTO_FLOOR, VLLM_DEFAULT_GMU, resolve_startup_gmu


def test_configured_override_wins_verbatim():
    profile = ModelProfileRecord(residency_source="calibrated", loaded_vram_mb=20_000.0, tensor_parallel_size=1)
    res = resolve_startup_gmu(
        explicit_gmu=0.83,
        loaded_vram_mb=20_000.0,
        tensor_parallel_size=1,
        per_gpu_total_mb=48_000.0,
        profile=profile,
    )
    assert res.source == "configured"
    assert res.value_for_spawn == 0.83
    assert res.value_for_admission == 0.83


def test_derived_matches_clamp_reconfigured_loaded_over_tp_total():
    profile = ModelProfileRecord(
        residency_source="calibrated",
        loaded_vram_mb=15_988.0,
        tensor_parallel_size=2,
        kv_budget_mb=4096.0,
    )
    # TP=1 with 4G KV: reconfigured = 15988 - 2*4096 + 1*4096 = 11892
    res = resolve_startup_gmu(
        explicit_gmu=None,
        loaded_vram_mb=15_988.0,
        tensor_parallel_size=1,
        per_gpu_total_mb=16_384.0,
        profile=profile,
        kv_cache_per_gpu_mb=4096.0,
    )
    assert res.source == "derived"
    assert res.value_for_spawn == pytest.approx(11_892 / 16_384)
    assert res.value_for_admission == res.value_for_spawn


def test_derived_clamped_to_floor_and_ceiling():
    tiny = ModelProfileRecord(residency_source="calibrated", loaded_vram_mb=1_000.0, tensor_parallel_size=1)
    res_lo = resolve_startup_gmu(
        explicit_gmu=None,
        loaded_vram_mb=1_000.0,
        tensor_parallel_size=1,
        per_gpu_total_mb=48_000.0,
        profile=tiny,
    )
    assert res_lo.value_for_admission == GMU_AUTO_FLOOR

    huge = ModelProfileRecord(residency_source="calibrated", loaded_vram_mb=100_000.0, tensor_parallel_size=1)
    res_hi = resolve_startup_gmu(
        explicit_gmu=None,
        loaded_vram_mb=100_000.0,
        tensor_parallel_size=1,
        per_gpu_total_mb=48_000.0,
        profile=huge,
    )
    assert res_hi.value_for_admission == GMU_AUTO_CEILING


def test_unknown_profile_omits_spawn_flag_admits_vllm_default():
    res = resolve_startup_gmu(
        explicit_gmu=None,
        loaded_vram_mb=None,
        tensor_parallel_size=1,
        per_gpu_total_mb=48_000.0,
        profile=None,
    )
    assert res.source == "default"
    assert res.value_for_spawn is None
    assert res.value_for_admission == VLLM_DEFAULT_GMU
