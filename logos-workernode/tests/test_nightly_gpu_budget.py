from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from logos_worker_node.lane_manager import LaneManager, NightlyGpuBudgetUnavailable, NightlyLaneIdentityMismatch
from logos_worker_node.logos_bridge import LogosBridgeClient
from logos_worker_node.model_profiles import ModelProfileRecord, ModelProfileRegistry
from logos_worker_node.models import (
    DeviceInfo,
    DeviceSummary,
    LaneConfig,
    LogosConfig,
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


@pytest.mark.asyncio
async def test_reserved_gpu_refuses_lifecycle_changes_but_preserves_other_lane() -> None:
    manager = _manager({0: TOTAL_MIB, 1: TOTAL_MIB})
    peer = MagicMock(lane_config=_config("org/peer"), destroy=AsyncMock(), wake_up=AsyncMock())
    manager._handles["peer"] = peer
    async with manager._lock.scoped(["benchmark"], [0]):
        manager._claim_nightly_gpus("benchmark", frozenset({0}))
    with pytest.raises(NightlyGpuBudgetUnavailable, match="cannot change its reserved GPUs"):
        manager._claim_nightly_gpus("benchmark", frozenset({1}))
    with pytest.raises(NightlyGpuBudgetUnavailable, match="already reserved"):
        manager._claim_nightly_gpus("another", frozenset({0}))
    with pytest.raises(RuntimeError, match="holding GPUs"):
        manager.begin_calibration_session()
    with pytest.raises(RuntimeError, match="nightly benchmark holds GPUs"):
        await manager.apply_lanes([_config("org/peer").model_copy(update={"lane_id": "peer"})])
    for operation in (manager.wake_lane, manager.sleep_lane, manager.remove_lane):
        with pytest.raises(RuntimeError, match="reserved for a nightly benchmark"):
            await operation("peer")
    assert manager._handles["peer"] is peer
    peer.destroy.assert_not_called()
    peer.wake_up.assert_not_called()
    assert not manager._transitions


@pytest.mark.asyncio
async def test_replacement_is_checked_against_live_memory_after_shutdown(monkeypatch) -> None:
    free = {0: EXPECTED_MIB - 1000}
    manager = _manager(free)
    original = _config()
    status = ProcessStatus(state=ProcessState.RUNNING, pid=15)

    async def destroy() -> None:
        free[0] = EXPECTED_MIB - 1

    manager._handles["benchmark"] = MagicMock(
        lane_config=original,
        status=MagicMock(return_value=status),
        destroy=AsyncMock(side_effect=destroy),
        close=AsyncMock(),
    )
    manager._port_alloc.allocate("benchmark")
    manager._nightly_process_gpu_vram = AsyncMock(return_value={"gpu0": 2_000})
    create = MagicMock()
    monkeypatch.setattr("logos_worker_node.lane_manager._create_handle", create)
    updates = {"vllm_config": {**original.vllm_config.model_dump(), "max_num_seqs": 16}}
    with pytest.raises(NightlyGpuBudgetUnavailable, match=f"needs {EXPECTED_MIB} MiB including warm-up"):
        await manager.reconfigure_lane("benchmark", updates, require_idle=True, nightly_gpu_budget=True)
    create.assert_not_called()
    assert "benchmark" not in manager._handles
    assert manager._nightly_gpu_holds == {"benchmark": frozenset({0})}


@pytest.mark.asyncio
async def test_nvidia_process_query_only_credits_the_lane_and_its_children(monkeypatch) -> None:
    manager = _manager({0: TOTAL_MIB, 1: TOTAL_MIB})
    result = b"\n".join(
        [
            b"gpu0, 21, 2500",
            b"gpu0, 22, 200",
            b"gpu1, 22, 1200",
            b"gpu0, 31, 8000",
            b"gpu0, 22, N/A",
            b"gpu0, 21, inf",
            b"gpu0, 21, -2",
            b"gpu0, invalid, 900",
            b"malformed",
        ]
    )
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(result, None)))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr("logos_worker_node.lane_manager.asyncio.create_subprocess_exec", execute)
    monkeypatch.setattr(LaneManager, "_read_parent_pid", staticmethod(lambda pid, _cache: 21 if pid == 22 else None))
    assert await manager._nightly_process_gpu_vram(None) == {}
    assert await manager._nightly_process_gpu_vram(21) == {"gpu0": 2_700, "gpu1": 1200}
    assert "--query-compute-apps=gpu_uuid,pid,used_memory" in execute.await_args.args


@pytest.mark.asyncio
async def test_nvidia_process_query_timeout_reaps_subprocess(monkeypatch) -> None:
    manager = _manager({0: TOTAL_MIB})
    process = SimpleNamespace(
        returncode=None, communicate=AsyncMock(side_effect=TimeoutError), kill=MagicMock(), wait=AsyncMock()
    )
    monkeypatch.setattr(
        "logos_worker_node.lane_manager.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    assert await manager._nightly_process_gpu_vram(21) == {}
    process.kill.assert_called_once_with()
    process.wait.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_nightly_bridge_preserves_budget_and_lane_identity_semantics() -> None:
    lane = MagicMock()
    lane.model_dump.return_value = {"lane_id": "benchmark", "model": MODEL}
    manager = SimpleNamespace(
        status_revision=1,
        add_lane=AsyncMock(return_value=lane),
        reconfigure_lane=AsyncMock(return_value=lane),
        remove_lane=AsyncMock(),
        check_nightly_gpu_budget=AsyncMock(return_value={0: EXPECTED_MIB}),
        release_nightly_gpu_budget=AsyncMock(),
    )
    bridge = LogosBridgeClient(SimpleNamespace(state=SimpleNamespace(lane_manager=manager)), LogosConfig())
    bridge._send_json = AsyncMock()

    async def send(action: str, **params):
        await bridge._execute_in_order(object(), action, action, {"lane_id": "benchmark", **params})
        return bridge._send_json.await_args.args[1]

    try:
        assert (await send("check_nightly_gpu_budget"))["result"]["required_mib_by_gpu"] == {0: EXPECTED_MIB}
        cold_params = {"lane_id": "benchmark", "model": MODEL, "vllm": True, "nightly_gpu_budget": True}
        assert bridge._command_target("add_lane", cold_params) == "benchmark"
        assert (await send("add_lane", model=MODEL, vllm=True, nightly_gpu_budget=True))["success"]
        assert manager.add_lane.await_args.kwargs == {"nightly_gpu_budget": True}
        assert "nightly_gpu_budget" not in manager.add_lane.await_args.args[0].model_dump()
        report = await send(
            "reconfigure_lane", updates={"vllm_config": {}}, force_restart=True, nightly_gpu_budget=True
        )
        assert report["result"]["restarted"]
        assert manager.reconfigure_lane.await_args.kwargs == {
            "require_idle": True,
            "force_restart": True,
            "nightly_gpu_budget": True,
        }
        manager.check_nightly_gpu_budget.side_effect = NightlyGpuBudgetUnavailable("no room")
        assert (await send("check_nightly_gpu_budget"))["reason_code"] == "nightly_gpu_budget"
        manager.remove_lane.side_effect = NightlyLaneIdentityMismatch("different model")
        assert (await send("delete_lane", expected_model=MODEL))["reason_code"] == "nightly_lane_identity"
        manager.remove_lane.assert_awaited_once_with("benchmark", expected_model=MODEL)
        assert not (await send("delete_lane", expected_model=4))["success"]
        assert (await send("release_nightly_gpu_budget"))["result"] == {"ok": True}
        manager.release_nightly_gpu_budget.assert_awaited_once_with("benchmark")
    finally:
        await bridge.stop()
