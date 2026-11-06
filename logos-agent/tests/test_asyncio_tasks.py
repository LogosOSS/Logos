import asyncio

import pytest
from app.asyncio_tasks import cancel_and_wait


async def test_cancels_and_waits_for_the_task():
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)

    await cancel_and_wait(task)

    assert task.cancelled()


async def test_nothing_to_stop():
    await cancel_and_wait(None)


async def test_a_task_that_already_finished_stays_finished():
    task = asyncio.create_task(asyncio.sleep(0))
    await task

    await cancel_and_wait(task)

    assert not task.cancelled()


async def test_a_failure_of_the_task_is_raised():
    async def _fails() -> None:
        raise RuntimeError("cleanup failed")

    task = asyncio.create_task(_fails())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await cancel_and_wait(task)


async def test_a_cancellation_of_the_caller_propagates():
    release = asyncio.Event()

    async def _slow_to_stop() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await release.wait()

    child = asyncio.create_task(_slow_to_stop())
    await asyncio.sleep(0)
    stopper = asyncio.create_task(cancel_and_wait(child))
    await asyncio.sleep(0)

    stopper.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    assert child.cancelled()
