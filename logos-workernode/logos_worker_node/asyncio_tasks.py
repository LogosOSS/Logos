"""Stopping the worker node's background asyncio tasks."""

from __future__ import annotations

import asyncio


async def cancel_and_wait(*tasks: asyncio.Future | None) -> None:
    """Cancel the given tasks and wait for all of them to finish.

    Their cancellation is the requested outcome and is not raised; the first
    other exception one of them ended with is, after all have finished. When
    the caller itself is cancelled meanwhile, that cancellation propagates.
    ``None`` entries are ignored.
    """
    live = [task for task in tasks if task is not None]
    for task in live:
        task.cancel()
    outcomes = await asyncio.gather(*live, return_exceptions=True)
    failure = next(
        (o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, asyncio.CancelledError)),
        None,
    )
    if failure is not None:
        raise failure


async def finish_shielded(task: asyncio.Future) -> None:
    """Wait for ``task`` to finish even when the caller is cancelled meanwhile.

    Cancelling the caller (once or repeatedly) never interrupts ``task``: the
    wait goes on until it is done. Its own exception, if any, is raised first;
    otherwise the caller's cancellation is re-raised once the task is done.
    """
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            await finish_shielded(task)
        task.result()
        raise
