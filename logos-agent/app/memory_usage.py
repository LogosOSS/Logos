"""Sample RAM across the live containers owned by each session."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

import httpx

from . import db, docker_engine
from .schemas import ACTIVE_STATUSES, SessionStatus

logger = logging.getLogger(__name__)
SAMPLE_INTERVAL_S = 2
MAX_SAMPLE_AGE_S = 10
_current: dict[int, int] = {}
_sampled_at = 0.0


def current(session_id: int, status: str) -> int | None:
    if status not in ACTIVE_STATUSES or status == SessionStatus.QUEUED or monotonic() - _sampled_at >= MAX_SAMPLE_AGE_S:
        return None
    return _current.get(session_id)


async def sample() -> None:
    global _current, _sampled_at
    started = monotonic()
    containers = await docker_engine.list_managed_containers(all_states=False)
    live = [
        (int(label), container["Id"])
        for container in containers
        if container["State"] in ("running", "paused")
        and (label := container["Labels"].get("logos.agent.session", "")).isdigit()
    ]
    slots = asyncio.Semaphore(8)

    async def read(container_id: str) -> int | None:
        async with slots:
            try:
                return await docker_engine.container_memory_usage(container_id)
            except (httpx.HTTPError, docker_engine.DockerError) as exc:
                logger.debug("Could not read memory of container %s: %s", container_id, exc)
                return None

    readings = await asyncio.gather(*(read(cid) for _, cid in live))
    totals: dict[int, int] = {}
    incomplete: set[int] = set()
    for (session_id, _), usage in zip(live, readings, strict=True):
        if isinstance(usage, int):
            totals[session_id] = totals.get(session_id, 0) + usage
        else:
            # A phase may exit during the sweep. A partial total is unknown.
            incomplete.add(session_id)
    for session_id in incomplete:
        totals.pop(session_id, None)
    if totals:
        await db.record_memory_peaks(totals)
    _current, _sampled_at = totals, started


async def monitor() -> None:
    try:
        while True:
            try:
                await sample()
            except Exception as exc:
                _current.clear()
                logger.warning("Could not sample session memory: %s", exc)
            await asyncio.sleep(SAMPLE_INTERVAL_S)
    finally:
        _current.clear()
