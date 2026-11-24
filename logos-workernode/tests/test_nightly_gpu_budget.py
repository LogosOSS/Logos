from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from logos_worker_node.lane_manager import LaneManager, NightlyGpuBudgetUnavailable, NightlyLaneIdentityMismatch
from logos_worker_node.model_profiles import ModelProfileRecord, ModelProfileRegistry
from logos_worker_node.models import (
    DeviceInfo,
    DeviceSummary,
    LaneConfig,
    ProcessState,
    ProcessStatus,
    VllmConfig,
    WorkerConfig,
)

MODEL = "org/model"
TOTAL_MIB = 95 * 1024
EXPECTED_MIB = 20_000 - 4 * 1024 + 2 * 1024 + 512


def _manager(free: dict[int, float]) -> LaneManager:
    async def snapshot() -> DeviceSummary:
        return DeviceSummary(
            timestamp=datetime.now(timezone.utc),
            mode="nvidia",
            nvidia_smi_available=True,
            devices=[
                DeviceInfo(device_id=f"gpu{gpu}", memory_total_mb=TOTAL_MIB, memory_free_mb=mb, extra={"index": gpu})
                for gpu, mb in free.items()
            ],
        )

    profiles = ModelProfileRegistry()
    profiles._profiles[MODEL] = ModelProfileRecord(
        residency_source="calibrated",
        tensor_parallel_size=1,
        loaded_vram_mb=20_000,
        base_residency_mb=20_000,
        calibrated_footprint_mb=20_000,
        calibrated_kv_mb=4096,
    )
    return LaneManager(
        WorkerConfig(gpu_devices="all"),
        model_profiles=profiles,
        gpu_device_count=lambda: len(free),
        per_gpu_vram_mb=lambda: TOTAL_MIB,
        gpu_snapshot=snapshot,
        gpu_force_poll=AsyncMock(),
    )


def _config(model: str = MODEL, gpu: str = "0", cache: str = "2G") -> LaneConfig:
    return LaneConfig(
        lane_id="benchmark",
        model=model,
        gpu_devices=gpu,
        vllm=True,
        vllm_config=VllmConfig(kv_cache_memory_bytes=cache, gpu_memory_utilization=0.16),
    )


@pytest.mark.asyncio
async def test_cold_nightly_launch_includes_warmup_margin_and_skips_without_room(monkeypatch) -> None:
    manager = _manager({0: EXPECTED_MIB - 1, 1: TOTAL_MIB})
    create = MagicMock()
    monkeypatch.setattr("logos_worker_node.lane_manager._create_handle", create)
    with pytest.raises(NightlyGpuBudgetUnavailable, match=f"GPU 0 .*needs {EXPECTED_MIB} MiB including warm-up"):
        await manager.add_lane(_config(), nightly_gpu_budget=True)
    create.assert_not_called()
    assert not manager._nightly_gpu_holds


@pytest.mark.asyncio
async def test_cold_nightly_launch_selects_only_a_gpu_that_can_cover_warmup(monkeypatch) -> None:
    manager = _manager({0: EXPECTED_MIB - 1, 1: EXPECTED_MIB + 128})
    new = MagicMock(
        init=AsyncMock(),
        spawn=AsyncMock(return_value=ProcessStatus(state=ProcessState.RUNNING, pid=15)),
    )
    manager._get_status_unlocked = AsyncMock()
    monkeypatch.setattr("logos_worker_node.lane_manager._create_handle", lambda *args, **kwargs: new)
    await manager.add_lane(_config(gpu=""), nightly_gpu_budget=True)
    assert manager._nightly_gpu_holds == {"benchmark": frozenset({1})}
    assert new.spawn.await_args.args[0].gpu_devices == "1"


@pytest.mark.asyncio
async def test_existing_lane_uses_only_its_own_measured_gpu_memory_for_credit() -> None:
    free = {0: EXPECTED_MIB - 1000, 1: TOTAL_MIB - 20_000}
    manager = _manager(free)
    manager._handles["benchmark"] = MagicMock(
        lane_config=_config(), status=MagicMock(return_value=ProcessStatus(state=ProcessState.RUNNING, pid=15))
    )
    manager._nightly_process_gpu_vram = AsyncMock(return_value={"gpu1": 20_000})
    with pytest.raises(NightlyGpuBudgetUnavailable, match=r"GPU 0 .*needs"):
        await manager.check_nightly_gpu_budget("benchmark")
    manager._nightly_process_gpu_vram.return_value = {"gpu0": 1500}
    assert await manager.check_nightly_gpu_budget("benchmark") == {0: EXPECTED_MIB}
    manager._nightly_process_gpu_vram.assert_awaited_with(15)
    assert manager._nightly_gpu_holds == {"benchmark": frozenset({0})}
    await manager.release_nightly_gpu_budget("benchmark")
    assert not manager._nightly_gpu_holds


@pytest.mark.asyncio
async def test_reconfigure_defers_before_stopping_original_lane() -> None:
    manager = _manager({0: EXPECTED_MIB - 1})
    original = _config()
    manager._handles["benchmark"] = MagicMock(
        lane_config=original, status=MagicMock(return_value=ProcessStatus(state=ProcessState.RUNNING, pid=15))
    )
    manager._nightly_process_gpu_vram = AsyncMock(return_value={})
    manager._restart_lane_unlocked = AsyncMock()
    updates = {"vllm_config": {**original.vllm_config.model_dump(), "max_num_seqs": 16}}
    with pytest.raises(NightlyGpuBudgetUnavailable, match=r"GPU 0 .*needs"):
        await manager.reconfigure_lane("benchmark", updates, require_idle=True, nightly_gpu_budget=True)
    manager._restart_lane_unlocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbounded_kv_cache_defers_benchmark() -> None:
    manager = _manager({0: TOTAL_MIB})
    with pytest.raises(NightlyGpuBudgetUnavailable, match="requires an explicit KV-cache budget"):
        await manager.add_lane(_config(cache=""), nightly_gpu_budget=True)


@pytest.mark.asyncio
async def test_cached_free_memory_is_ignored_when_gpu_poll_fails() -> None:
    manager = _manager({0: TOTAL_MIB})
    manager._gpu_force_poll.return_value = False
    with pytest.raises(NightlyGpuBudgetUnavailable, match="fresh GPU telemetry is unavailable"):
        await manager.add_lane(_config(), nightly_gpu_budget=True)


@pytest.mark.asyncio
async def test_benchmark_cannot_remove_another_model_at_its_temporary_lane_id() -> None:
    manager = _manager({0: TOTAL_MIB})
    unrelated = MagicMock(lane_config=_config("org/unrelated"))
    manager._handles["benchmark"] = unrelated
    with pytest.raises(NightlyLaneIdentityMismatch, match="does not serve model"):
        await manager.remove_lane("benchmark", expected_model=MODEL)
    assert manager._handles["benchmark"] is unrelated
    unrelated.destroy.assert_not_called()
