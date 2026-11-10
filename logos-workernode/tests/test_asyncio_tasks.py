from __future__ import annotations

import asyncio

import pytest

from logos_worker_node.asyncio_tasks import cancel_and_wait, finish_shielded


def _run(coro):
    return asyncio.run(coro)


def test_cancel_and_wait_stops_every_task_and_skips_none():
    async def scenario():
        tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(3)]
        await asyncio.sleep(0)
        await cancel_and_wait(tasks[0], None, *tasks[1:])
        return tasks

    assert all(task.cancelled() for task in _run(scenario()))


def test_cancel_and_wait_without_tasks_returns():
    _run(cancel_and_wait())


def test_cancel_and_wait_raises_what_a_task_failed_with():
    async def broken():
        raise KeyError("lost")

    async def scenario():
        done = asyncio.create_task(asyncio.sleep(0))
        failed = asyncio.create_task(broken())
        await asyncio.sleep(0)
        await cancel_and_wait(done, failed)

    with pytest.raises(KeyError):
        _run(scenario())


def test_cancel_and_wait_lets_the_callers_cancellation_through():
    async def scenario():
        release = asyncio.Event()

        async def lingering():
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()

        child = asyncio.create_task(lingering())
        await asyncio.sleep(0)
        stopper = asyncio.create_task(cancel_and_wait(child))
        await asyncio.sleep(0)
        stopper.cancel()
        # The stopper still waits for the child to finish, then propagates
        # its own cancellation instead of returning normally.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert child.cancelled()

    _run(scenario())


def test_finish_shielded_returns_when_not_cancelled():
    async def scenario():
        task = asyncio.create_task(asyncio.sleep(0))
        await finish_shielded(task)
        return task

    assert _run(scenario()).done()


def test_finish_shielded_raises_the_tasks_own_failure():
    async def broken():
        raise ValueError("cleanup failed")

    async def scenario():
        await finish_shielded(asyncio.create_task(broken()))

    with pytest.raises(ValueError, match="cleanup failed"):
        _run(scenario())


def test_finish_shielded_completes_the_task_through_repeated_cancellation():
    async def scenario():
        release = asyncio.Event()
        finished = []

        async def cleanup():
            await release.wait()
            finished.append(True)

        task = asyncio.create_task(cleanup())
        waiter = asyncio.create_task(finish_shielded(task))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done()  # still waiting for the cleanup
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        return finished

    assert _run(scenario()) == [True]


def test_finish_shielded_prefers_the_tasks_failure_over_the_cancellation():
    async def scenario():
        release = asyncio.Event()

        async def cleanup():
            await release.wait()
            raise RuntimeError("cleanup failed")

        task = asyncio.create_task(cleanup())
        waiter = asyncio.create_task(finish_shielded(task))
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        release.set()
        await waiter

    with pytest.raises(RuntimeError, match="cleanup failed"):
        _run(scenario())
