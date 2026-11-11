"""Detach client cancellation from bounded upstream usage collection."""

import asyncio
import logging
import os
from contextlib import suppress

from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)
_background_drains: set[asyncio.Task] = set()


class UsageDrainIterator:
    """Keep a started provider iterator alive after its client leaves.

    Only one read is outstanding. Shielding it preserves the upstream's read
    context when ASGI cancels the client task, including between chunks.
    """

    def __init__(self, source, accumulator, timeout=None, claim_prefetched=None):
        self.claim_prefetched = claim_prefetched
        self.source = source
        self.accumulator = accumulator
        self.timeout = float(os.getenv("LOGOS_STREAM_DRAIN_TIMEOUT_SECONDS", "120")) if timeout is None else timeout
        self.pending = None
        self.drain_task = None
        self.finished = False
        self.started = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.finished or self.drain_task is not None:
            raise StopAsyncIteration
        self.started = True
        self.pending = asyncio.create_task(anext(self.source))
        try:
            result = await asyncio.shield(self.pending)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except BaseException:
            self.finished = True
            self.pending = None
            raise
        self.pending = None
        return result

    async def aclose(self):
        """Release the client immediately; a background task owns finalization."""
        if self.finished or self.drain_task is not None:
            return
        if not self.started and self.claim_prefetched is None:
            self.finished = True
            await self.source.aclose()
            return
        if self.claim_prefetched is not None:
            self.claim_prefetched()
        self.accumulator.client_disconnected = True
        self.drain_task = asyncio.create_task(self._drain())
        _background_drains.add(self.drain_task)
        self.drain_task.add_done_callback(_background_drains.discard)

    async def _drain(self):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, self.timeout)
        # Mark the deadline before cancellation unwinds the source's billing
        # finally: silence at a timeout does not prove an empty provider response.
        marker = loop.call_at(deadline, setattr, self.accumulator, "drain_timed_out", True)
        try:
            async with asyncio.timeout_at(deadline):
                if self.pending is not None:
                    if (
                        self.accumulator.has_final_usage()
                        or self.accumulator.terminal_event_received
                        or self.accumulator.upstream_error
                    ):
                        self.pending.cancel()
                        with suppress(asyncio.CancelledError, StopAsyncIteration):
                            await self.pending
                    else:
                        await self.pending
                    self.pending = None
                while not (
                    self.accumulator.has_final_usage()
                    or self.accumulator.terminal_event_received
                    or self.accumulator.upstream_error
                ):
                    await anext(self.source)
        except (StopAsyncIteration, TimeoutError):
            pass
        except Exception:
            logger.exception("Upstream usage drain failed")
        finally:
            marker.cancel()
            if self.pending is not None and not self.pending.done():
                self.pending.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.pending
            await self.source.aclose()
            self.finished = True


class UsageDrainResponse(StreamingResponse):
    """Close the client iterator even when ASGI send fails between reads."""

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()
