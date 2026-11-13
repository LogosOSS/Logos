"""A queued local request asks the capacity planner for its model in the background.

The scheduler does not wait for the planner, but it holds the task until it
finishes: the event loop keeps only a weak reference to a task, and an
untracked request could be collected mid-run.
"""

from __future__ import annotations

import asyncio

from logos.pipeline.correcting_scheduler import ClassificationCorrectingScheduler


async def test_the_capacity_request_reaches_the_planner_and_is_held_until_done():
    scheduler = ClassificationCorrectingScheduler.__new__(ClassificationCorrectingScheduler)
    requested: list[tuple[str, int]] = []
    release = asyncio.Event()

    async def on_capacity_needed(model_name, provider_id):
        requested.append((model_name, provider_id))
        await release.wait()

    scheduler._on_capacity_needed = on_capacity_needed

    scheduler._request_capacity("qwen-27b", 4)

    (task,) = scheduler._capacity_needed_tasks
    assert task.get_name() == "capacity-needed-qwen-27b"
    await asyncio.sleep(0)
    assert requested == [("qwen-27b", 4)]
    release.set()
    await task
    assert not scheduler._capacity_needed_tasks
