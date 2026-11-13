"""Stopping the runner's background asyncio tasks."""

from __future__ import annotations

import asyncio


async def cancel_and_wait(task: asyncio.Future | None) -> None:
    """Cancel ``task`` and wait until it has finished; ``None`` is a no-op.

    Ending in the cancellation requested here is the expected outcome and
    raises nothing; any other exception the task ended with is raised. A
    cancellation of the caller while it waits propagates once the task is done.
    """
    if task is None:
        return
    task.cancel()
    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
    if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
        raise outcome
