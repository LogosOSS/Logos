"""Worker status → registry.update_runtime → planner preserves startup GMU keys."""

from __future__ import annotations

import pytest

from logos.capacity.capacity_planner import CapacityPlanner
from logos.logosnode_registry import LogosNodeRuntimeRegistry, ProviderSession
from logos.sdi.models import ModelProfile

PROVIDER = 42
MODEL = "google/gemma-3-4b-it"


@pytest.mark.asyncio
async def test_update_runtime_preserves_startup_gmu_and_configured_vllm():
    registry = LogosNodeRuntimeRegistry()
    registry._sessions[PROVIDER] = ProviderSession(  # noqa: SLF001
        provider_id=PROVIDER, worker_id="worker-gmu", websocket=object()
    )

    runtime = {
        "lanes": [],
        "configured_vllm_by_model": {
            MODEL: {"gpu_memory_utilization": 0.83, "kv_cache_memory_bytes": "13G"},
        },
        "startup_gmu_by_model": {
            MODEL: {"gpu_memory_utilization": 0.83, "source": "configured"},
        },
        "devices": {"devices": []},
    }
    await registry.update_runtime(PROVIDER, runtime)
    snap = registry.peek_runtime_snapshot(PROVIDER)
    assert snap is not None
    rt = snap["runtime"]
    assert rt["configured_vllm_by_model"][MODEL]["gpu_memory_utilization"] == 0.83
    assert rt["startup_gmu_by_model"][MODEL] == {
        "gpu_memory_utilization": 0.83,
        "source": "configured",
    }

    planner = CapacityPlanner(logosnode_facade=None, logosnode_registry=registry, demand_tracker=None)
    profile = ModelProfile(
        model_name=MODEL,
        engine="vllm",
        residency_source="calibrated",
        loaded_vram_mb=10_000.0,
        observed_gpu_memory_utilization=None,
        min_gpu_memory_utilization_to_load=None,
    )
    assert planner._startup_gmu_for_model(PROVIDER, MODEL, profile) == 0.83  # noqa: SLF001
