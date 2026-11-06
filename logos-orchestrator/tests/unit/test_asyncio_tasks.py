import asyncio

import pytest

from logos.asyncio_tasks import cancel_and_wait


async def test_a_running_task_is_cancelled_and_awaited():
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)

    await cancel_and_wait(task)

    assert task.cancelled()


async def test_no_task_is_a_no_op():
    await cancel_and_wait(None)


async def test_a_finished_task_is_left_as_it_ended():
    task = asyncio.create_task(asyncio.sleep(0, result="done"))
    await task

    await cancel_and_wait(task)

    assert task.result() == "done"


async def test_the_exception_a_task_ended_with_is_raised():
    async def _broken() -> None:
        raise ValueError("broken")

    task = asyncio.create_task(_broken())
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="broken"):
        await cancel_and_wait(task)


async def test_the_callers_own_cancellation_is_not_absorbed():
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
