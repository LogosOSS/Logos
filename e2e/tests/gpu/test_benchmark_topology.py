"""Dual-node, dual-Ada hardware against the real worker + gpusim.

Two RTX 6000 Ada cards, sleep/nosleep argv variants, KV vs GMU CLI parity, and
TP=2 claiming both devices. Asserts transitions and ledger invariants — not
wall-clock GPU startup speed. Results are synthetic; no real results dir.
"""

from __future__ import annotations

import pytest
from harness import lane as lane_harness
from harness.benchmark_topology import (
    BENCHMARK_ENGINE_SPECS,
    BENCHMARK_GPU_PROFILE,
    BENCHMARK_IDLE_SLEEP_L1_S,
    BENCHMARK_MODEL_PROFILES,
    BENCHMARK_MODEL_REVISIONS,
    BENCHMARK_MODELS_5,
    BENCHMARK_REQUEST_TEMPLATES,
    TIMING_MODE,
    dual_ada_node,
    engine_vllm_overrides,
)
from harness.gpusim.scenario import GpuProfile, VllmScript

ADA = GpuProfile.load(BENCHMARK_GPU_PROFILE)


async def test_dual_ada_tp2_claims_both_gpus(gpu_sim, lane):
    env = gpu_sim(dual_ada_node())
    async with lane() as handle:
        await handle.spawn(
            lane_harness.lane_config(
                model=BENCHMARK_MODELS_5[0],
                tensor_parallel_size=2,
                gpu_memory_utilization=0.85,
            )
        )
        assert env.arg_value("--tensor-parallel-size") == "2"
        for index in (0, 1):
            assert env.used_mb(index) == pytest.approx(ADA.memory_total_mb * 0.85, rel=0.01)
        await handle.stop()
        for index in (0, 1):
            assert env.used_mb(index) == 0.0


async def test_both_gpus_claimed_blocks_second_tp2_ledger(gpu_sim, lane):
    """After a TP=2 lane holds both cards, free VRAM on each is below another TP=2 claim."""
    env = gpu_sim(dual_ada_node())
    async with lane(lane_id="lane-a") as first:
        await first.spawn(
            lane_harness.lane_config(
                model=BENCHMARK_MODELS_5[1],
                tensor_parallel_size=2,
                gpu_memory_utilization=0.9,
            )
        )
        claimed = [env.used_mb(i) for i in (0, 1)]
        assert all(mb > ADA.memory_total_mb * 0.8 for mb in claimed)
        free = [env.free_mb(i) for i in (0, 1)]
        assert all(f < ADA.memory_total_mb * 0.2 for f in free)


@pytest.mark.parametrize("enable_sleep", [True, False])
async def test_sleep_mode_argv_follows_lane_config(gpu_sim, lane, enable_sleep):
    env = gpu_sim(dual_ada_node())
    async with lane() as handle:
        await handle.spawn(
            lane_harness.lane_config(
                model=BENCHMARK_MODELS_5[2],
                enable_sleep_mode=enable_sleep,
                tensor_parallel_size=1,
            )
        )
        argv = env.last_argv()
        has_flag = "--enable-sleep-mode" in argv
        assert has_flag is enable_sleep


async def test_kv_cache_bytes_without_explicit_gmu_omits_gmu_flag(gpu_sim, lane):
    """KV-sized lanes must not inject a default GMU.

    GMU acts as a startup guard only when KV is explicit; an operator override
    of GMU still wins when set.
    """
    env = gpu_sim(dual_ada_node(), VllmScript(vram_mb=12_000.0))
    async with lane() as handle:
        await handle.spawn(
            lane_harness.lane_config(
                model=BENCHMARK_MODELS_5[3],
                kv_cache_memory_bytes="8589934592",
            )
        )
        assert env.arg_value("--kv-cache-memory-bytes") == "8589934592"
        assert env.arg_value("--gpu-memory-utilization") is None


async def test_pinned_revisions_cover_every_benchmark_model():
    assert set(BENCHMARK_MODELS_5) == set(BENCHMARK_MODEL_REVISIONS)
    for model, revision in BENCHMARK_MODEL_REVISIONS.items():
        assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision), model


async def test_qwen_request_template_disables_thinking():
    tmpl = BENCHMARK_REQUEST_TEMPLATES[BENCHMARK_MODELS_5[0]]
    assert tmpl["chat_template_kwargs"]["enable_thinking"] is False


async def test_idle_sleep_threshold_constant():
    """Documented orchestrator override — not the image default of 1800 s."""
    assert BENCHMARK_IDLE_SLEEP_L1_S == 60.0


def test_public_defaults_use_real_kv_and_context_not_uniform_8192():
    """CI public defaults must carry calibrated KV/ctx, not a 4 GiB/8192 stand-in."""
    assert TIMING_MODE == "accelerated_functional"
    qwen = BENCHMARK_MODEL_PROFILES["Qwen/Qwen3.6-35B-A3B"]
    assert qwen["kv_budget_mb"] == 4096.0
    assert qwen["max_context_length"] == 262_144
    assert qwen["tensor_parallel_size"] == 2
    llama = BENCHMARK_MODEL_PROFILES["meta-llama/Llama-3.1-8B-Instruct"]
    assert llama["kv_budget_mb"] == 23_552.0
    assert llama["max_context_length"] == 131_072
    # Must not collapse every model to the old placeholder.
    ctxs = {BENCHMARK_MODEL_PROFILES[m]["max_context_length"] for m in BENCHMARK_MODELS_5}
    assert ctxs != {8192}


def test_gemma4_engine_kv_pin_differs_from_calibration_profile():
    """Frozen engine pin (~13 GiB) ≠ calibration profile KV (~30 GiB)."""
    calib = BENCHMARK_MODEL_PROFILES["google/gemma-3-4b-it"]["kv_budget_mb"]
    assert calib == 30_720.0
    assert "google/gemma-3-4b-it" in BENCHMARK_ENGINE_SPECS
    overrides = engine_vllm_overrides("google/gemma-3-4b-it")
    engine_bytes = int(overrides["kv_cache_memory_bytes"])
    engine_mb = engine_bytes / (1024.0 * 1024.0)
    assert abs(engine_mb - 13_312.0) < 1.0  # 13958643712 bytes
    assert engine_mb < calib * 0.6
    assert float(overrides["gpu_memory_utilization"]) == 0.5


def test_engine_overrides_cover_all_five_models():
    assert set(BENCHMARK_ENGINE_SPECS) == set(BENCHMARK_MODELS_5)
    for model in BENCHMARK_MODELS_5:
        ov = engine_vllm_overrides(model)
        assert "kv_cache_memory_bytes" in ov
        assert "tensor_parallel_size" in ov
        assert "gpu_memory_utilization" in ov


async def test_sleep_wake_round_trip_on_ada(gpu_sim, lane):
    env = gpu_sim(dual_ada_node())
    async with lane() as handle:
        await handle.spawn(
            lane_harness.lane_config(
                model=BENCHMARK_MODELS_5[4],
                enable_sleep_mode=True,
                gpu_memory_utilization=0.85,
            )
        )
        awake = env.used_mb(0)
        assert await handle.is_sleeping() is False
        await handle.sleep(level=1)
        assert await handle.is_sleeping() is True
        # Ledger must shrink so worker sleeping_residual ≠ awake footprint.
        assert env.used_mb(0) < awake * 0.2
        await handle.wake_up()
        assert await handle.is_sleeping() is False
