"""Bound worker stream queues and account for their shared session memory."""

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any


def _limit(name: str, default: int) -> int:
    return max(1, int(os.getenv(name, str(default))))


@dataclass
class WorkerStreamBudget:
    """Limits and buffered byte count shared by every stream of one session."""

    max_chunks: int = field(default_factory=lambda: _limit("LOGOSNODE_STREAM_MAX_CHUNKS", 512))
    max_stream_bytes: int = field(default_factory=lambda: _limit("LOGOSNODE_STREAM_MAX_BYTES", 8 * 1024 * 1024))
    max_chunk_bytes: int = field(default_factory=lambda: _limit("LOGOSNODE_STREAM_MAX_CHUNK_BYTES", 1024 * 1024))
    max_session_bytes: int = field(
        default_factory=lambda: _limit("LOGOSNODE_SESSION_MAX_STREAM_BYTES", 64 * 1024 * 1024)
    )
    buffered_bytes: int = 0


class WorkerStreamOverflow(RuntimeError):
    """A worker exceeded a stream or session buffer limit."""


class WorkerStreamBuffer:
    """Nonblocking ingress with one reserved terminal slot and byte accounting.

    All operations run on the registry's event loop. Limits are checked and
    counters updated without yielding, so streams share the budget atomically.
    """

    def __init__(self, budget: WorkerStreamBudget) -> None:
        self.budget = budget
        self.buffered_bytes = 0
        self.finished = False
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=budget.max_chunks + 1)

    def put_chunk(self, chunk: bytes) -> None:
        """Enqueue decoded bytes or raise WorkerStreamOverflow without waiting."""
        if self.finished:
            return
        size = len(chunk)
        if size > self.budget.max_chunk_bytes:
            raise WorkerStreamOverflow("chunk byte limit exceeded")
        if self._queue.qsize() >= self.budget.max_chunks:
            raise WorkerStreamOverflow("stream chunk count limit exceeded")
        if self.buffered_bytes + size > self.budget.max_stream_bytes:
            raise WorkerStreamOverflow("stream byte limit exceeded")
        if self.budget.buffered_bytes + size > self.budget.max_session_bytes:
            raise WorkerStreamOverflow("session stream byte limit exceeded")
        self._queue.put_nowait({"type": "stream_chunk", "chunk": chunk})
        self.buffered_bytes += size
        self.budget.buffered_bytes += size

    async def get(self) -> dict[str, Any]:
        """Release buffered-byte reservations as the consumer takes an event."""
        event = await self._queue.get()
        if event["type"] == "stream_chunk":
            size = len(event["chunk"])
            self.buffered_bytes -= size
            self.budget.buffered_bytes -= size
        return event

    def finish(
        self,
        success: bool,
        error: Any = None,
        *,
        discard: bool = False,
        status_code: Any = None,
        body: Any = None,
    ) -> None:
        """Wake the consumer even when full; optionally discard queued data."""
        if discard:
            self.clear()
        elif self.finished:
            return
        self.finished = True
        self._queue.put_nowait(
            {"type": "stream_end", "success": success, "error": error, "status_code": status_code, "body": body}
        )

    def clear(self) -> None:
        """Release all queued bytes on cancellation, disconnect or overflow."""
        self.budget.buffered_bytes -= self.buffered_bytes
        self.buffered_bytes = 0
        while not self._queue.empty():
            self._queue.get_nowait()
