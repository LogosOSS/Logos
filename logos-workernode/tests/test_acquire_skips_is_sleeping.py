"""Infer acquire must not await vLLM /is_sleeping (issue #165).

Sleep readiness on the hot path comes from lifecycle-tracked state
(``_known_sleep_state``), updated by sleep_lane / wake_lane and by background
status builds. Awaiting the EngineCore probe on every acquire added ~0.5–0.6 s
TTFT even on already-awake lanes.
"""

from __future__ import annotations

from typing import Any

import pytest

from logos_worker_node.lane_manager import LaneManager, LaneNotServingError
from logos_worker_node.models import LaneConfig, ProcessState, ProcessStatus, VllmConfig, WorkerConfig


class _ProbeHandle:
    def __init__(self, lane_id: str, port: int, lane: LaneConfig) -> None:
        self.lane_id = lane_id
        self.port = port
        self.lane_config = lane
        self.is_sleeping_calls = 0
        self.model_probe_count = 0
        self._sleeping = False

    def status(self) -> ProcessStatus:
        return ProcessStatus(state=ProcessState.RUNNING, pid=None)

    async def get_loaded_models(self) -> list[dict[str, Any]]:
        self.model_probe_count += 1
        return [{"name": self.lane_config.model, "size": 0, "size_vram": 0}]

    async def get_backend_metrics(self) -> dict[str, Any]:
        return {"engine": "vllm", "requests_running": 0}

    async def is_sleeping(self) -> bool | None:
        self.is_sleeping_calls += 1
        return self._sleeping

    async def sleep(self, level: int = 1, mode: str = "wait") -> dict[str, Any]:
        self._sleeping = True
        return {}

    async def wake_up(self) -> dict[str, Any]:
        self._sleeping = False
        return {}


def _manager() -> LaneManager:
    return LaneManager(WorkerConfig(), lane_port_start=15100, lane_port_end=15110)


def _inject(manager: LaneManager, *, sleeping: bool = False) -> tuple[str, _ProbeHandle]:
    lane_id = "bench-model"
    lane = LaneConfig(model="bench-model", vllm=True, vllm_config=VllmConfig(enable_sleep_mode=True))
    handle = _ProbeHandle(lane_id, 15100, lane)
    handle._sleeping = sleeping
    manager._handles[lane_id] = handle  # noqa: SLF001
    manager._lane_status_ttl_seconds = 0.0  # noqa: SLF001 — force rebuild every call
    manager._remember_sleep_state(lane_id, "sleeping" if sleeping else "awake")  # noqa: SLF001
    return lane_id, handle


@pytest.mark.asyncio
async def test_acquire_does_not_call_is_sleeping() -> None:
    manager = _manager()
    lane_id, handle = _inject(manager)

    status = await manager.acquire_lane_for_infer(lane_id)
    await manager.decrement_active_requests(lane_id)

    assert handle.is_sleeping_calls == 0
    assert status.sleep_state == "awake"
    assert status.runtime_state in {"loaded", "running"}


@pytest.mark.asyncio
async def test_background_status_still_probes_is_sleeping() -> None:
    manager = _manager()
    lane_id, handle = _inject(manager)

    await manager._get_status_unlocked(lane_id, probe_sleep=True)  # noqa: SLF001

    assert handle.is_sleeping_calls == 1


@pytest.mark.asyncio
async def test_acquire_rejects_lifecycle_sleeping_lane_without_probe() -> None:
    manager = _manager()
    lane_id, handle = _inject(manager, sleeping=True)

    with pytest.raises(LaneNotServingError, match="sleep_state=sleeping"):
        await manager.acquire_lane_for_infer(lane_id)

    assert handle.is_sleeping_calls == 0


@pytest.mark.asyncio
async def test_sleep_then_wake_updates_known_state_for_acquire() -> None:
    manager = _manager()
    lane_id, handle = _inject(manager)
    manager._lane_status_ttl_seconds = 0.0  # noqa: SLF001

    # Simulate sleep_lane's authoritative mark (full sleep_lane needs more
    # GPU/lock setup than this unit covers).
    handle._sleeping = True
    manager._remember_sleep_state(lane_id, "sleeping")  # noqa: SLF001
    manager.mark_status_dirty()

    with pytest.raises(LaneNotServingError):
        await manager.acquire_lane_for_infer(lane_id)

    handle._sleeping = False
    manager._remember_sleep_state(lane_id, "awake")  # noqa: SLF001
    manager.mark_status_dirty()

    status = await manager.acquire_lane_for_infer(lane_id)
    await manager.decrement_active_requests(lane_id)
    assert status.sleep_state == "awake"
    assert handle.is_sleeping_calls == 0


@pytest.mark.asyncio
async def test_acquire_rejects_while_wake_transition_is_in_flight() -> None:
    manager = _manager()
    lane_id, handle = _inject(manager)
    manager._set_transition(lane_id, "waking")  # noqa: SLF001

    with pytest.raises(LaneNotServingError, match="not routable"):
        await manager.acquire_lane_for_infer(lane_id)

    assert handle.is_sleeping_calls == 0


@pytest.mark.asyncio
async def test_delayed_sleep_probe_does_not_overwrite_newer_wake() -> None:
    """A probe that sampled sleeping before wake must not land after wake."""
    import asyncio

    manager = _manager()
    lane_id, handle = _inject(manager, sleeping=True)
    release = asyncio.Event()
    sampled = asyncio.Event()
    sample_value: list[bool] = []

    async def _slow_is_sleeping() -> bool | None:
        sample_value.append(handle._sleeping)
        sampled.set()
        await release.wait()
        return sample_value[0]

    handle.is_sleeping = _slow_is_sleeping  # type: ignore[method-assign]

    probe = asyncio.create_task(manager._get_status_unlocked(lane_id, probe_sleep=True))  # noqa: SLF001
    await sampled.wait()
    assert sample_value == [True]

    handle._sleeping = False
    manager._remember_sleep_state(lane_id, "awake")  # noqa: SLF001 — wake wins
    release.set()
    status = await probe

    assert manager._known_sleep_state[lane_id] == "awake"  # noqa: SLF001
    assert status.sleep_state == "awake"


@pytest.mark.asyncio
async def test_delayed_awake_probe_does_not_overwrite_newer_sleep() -> None:
    """A probe that sampled awake before sleep must not clear a later sleep."""
    import asyncio

    manager = _manager()
    lane_id, handle = _inject(manager, sleeping=False)
    release = asyncio.Event()
    sampled = asyncio.Event()
    sample_value: list[bool] = []

    async def _slow_is_sleeping() -> bool | None:
        sample_value.append(handle._sleeping)
        sampled.set()
        await release.wait()
        return sample_value[0]

    handle.is_sleeping = _slow_is_sleeping  # type: ignore[method-assign]

    probe = asyncio.create_task(manager._get_status_unlocked(lane_id, probe_sleep=True))  # noqa: SLF001
    await sampled.wait()
    assert sample_value == [False]

    handle._sleeping = True
    manager._remember_sleep_state(lane_id, "sleeping")  # noqa: SLF001
    release.set()
    await probe

    assert manager._known_sleep_state[lane_id] == "sleeping"  # noqa: SLF001
    with pytest.raises(LaneNotServingError, match="sleep_state=sleeping"):
        await manager.acquire_lane_for_infer(lane_id)
