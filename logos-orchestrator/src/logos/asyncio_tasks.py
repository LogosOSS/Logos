"""Stopping a background asyncio task."""

from __future__ import annotations

import asyncio


async def cancel_and_wait(task: asyncio.Future | None) -> None:
    """Cancel ``task`` (when there is one) and wait until it has finished.

    The cancellation requested here is the expected end of the task and raises
    nothing; any other exception the task ended with does. A cancellation of
    the caller while it waits propagates instead of being absorbed.
    """
    if task is None:
        return
    task.cancel()
    (result,) = await asyncio.gather(task, return_exceptions=True)
    if isinstance(result, asyncio.CancelledError):
        return
    if isinstance(result, BaseException):
        raise result
