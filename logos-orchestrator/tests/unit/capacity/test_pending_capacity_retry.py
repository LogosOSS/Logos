"""A pending capacity request is retried once freed VRAM may suffice.

The retry runs in the background. The planner holds its task until it
finishes, because the event loop keeps only a weak reference to a task and
an untracked retry could be collected mid-run.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from logos.capacity.capacity_planner import CapacityPlanner


def _planner(pending: dict[str, tuple[int, float]], lanes: list) -> tuple[CapacityPlanner, list, asyncio.Event]:
    planner = CapacityPlanner.__new__(CapacityPlanner)
    planner._pending_capacity = dict(pending)
    planner._facade = SimpleNamespace(get_provider_name=lambda provider_id: f"worker-{provider_id}")
    planner._safe_get_lanes = lambda provider_id: lanes
    prepared: list[tuple[int, str, float]] = []
    release = asyncio.Event()

    async def prepare_lane_for_request(provider_id, model_name, timeout_seconds):
        prepared.append((provider_id, model_name, timeout_seconds))
        await release.wait()

    planner.prepare_lane_for_request = prepare_lane_for_request
    return planner, prepared, release


async def test_a_pending_model_is_prepared_again_and_its_task_held_until_done():
    planner, prepared, release = _planner({"qwen-27b": (1, time.time())}, lanes=[])

    planner._retry_pending_capacity(1)

    assert "qwen-27b" not in planner._pending_capacity
    (task,) = planner._pending_capacity_retries
    await asyncio.sleep(0)
    assert prepared == [(1, "qwen-27b", 30.0)]
    release.set()
    await task
    assert not planner._pending_capacity_retries


async def test_a_model_that_already_has_a_ready_lane_is_not_retried():
    ready = SimpleNamespace(model_name="qwen-27b", runtime_state="loaded", sleep_state="awake")
    planner, prepared, _release = _planner({"qwen-27b": (1, time.time())}, lanes=[ready])

    planner._retry_pending_capacity(1)

    assert "qwen-27b" not in planner._pending_capacity
    assert not planner.__dict__.get("_pending_capacity_retries")
    assert prepared == []
