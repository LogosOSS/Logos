"""How many sessions the host's memory can hold at once.

Every session container, and every helper container a session runs in its
place, may use up to ``LOGOS_AGENT_SESSION_MEMORY_MB``. A paused session
keeps its memory too. The admission signal of the runner is spare *serving*
capacity, which says nothing about the host the containers run on, so the
parallel ceiling alone can promise sessions far more memory than the host
has: ten sessions of 4 GB on a host with 11 GB. When they all build at once,
the kernel kills processes of the host, not only of the sessions.

The runner therefore caps the parallel ceiling by a memory budget: the
memory all sessions together may claim. ``LOGOS_AGENT_MEMORY_BUDGET_MB`` sets
it explicitly; otherwise it is ``LOGOS_AGENT_HOST_MEMORY_SHARE`` of the
memory the Docker host reports, because the runner usually shares its host
with the database and the services of Logos. The ceiling in force is the
smaller of the configured (or operator) ceiling and the budget divided by the
memory of one session.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from . import docker_engine
from .config import settings

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass(frozen=True)
class Budget:
    """The memory budget of the sessions and the ceiling it allows."""

    budget_mb: int
    session_mb: int
    host_mb: int | None
    source: str

    @property
    def ceiling(self) -> int:
        if self.session_mb <= 0:
            return 0
        return max(0, self.budget_mb // self.session_mb)

    def describe(self) -> str:
        host = f" of {self.host_mb} MB on the host" if self.host_mb is not None else ""
        return (
            f"{self.budget_mb} MB{host} ({self.source}) holds {self.ceiling} "
            f"session(s) of {self.session_mb} MB"
        )


_current: Budget | None = None


def compute(host_mb: int | None) -> Budget | None:
    """The budget from the settings and the host's memory, or None when neither gives one."""
    session_mb = settings.session_memory_mb
    if settings.memory_budget_mb > 0:
        return Budget(settings.memory_budget_mb, session_mb, host_mb, "LOGOS_AGENT_MEMORY_BUDGET_MB")
    if host_mb is None or host_mb <= 0:
        return None
    share = min(max(settings.host_memory_share, 0.0), 1.0)
    return Budget(int(host_mb * share), session_mb, host_mb, f"{share:.0%} of the host")


def _local_memory_mb() -> int | None:
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // _MB)
    except (ValueError, OSError, AttributeError):
        return None


async def host_memory_mb() -> int | None:
    """The memory of the host that runs the session containers."""
    try:
        info = (await docker_engine._request("GET", "/info")).json()
        total = int(info.get("MemTotal") or 0)
        if total > 0:
            return total // _MB
    except Exception as exc:  # noqa: BLE001 - any failure falls back to this machine's memory
        logger.warning("could not read the Docker host's memory: %s", exc)
    return _local_memory_mb()


async def establish() -> Budget | None:
    """Read the host once and fix the budget for the life of the process."""
    global _current
    _current = compute(await host_memory_mb())
    if _current is None:
        logger.warning(
            "no memory budget: the host's memory is unknown and LOGOS_AGENT_MEMORY_BUDGET_MB is not set; "
            "only LOGOS_AGENT_MAX_PARALLEL_SESSIONS limits sessions"
        )
    elif _current.ceiling < settings.max_parallel_sessions:
        logger.warning(
            "memory budget %s; the parallel ceiling is lowered from %s to %s",
            _current.describe(),
            settings.max_parallel_sessions,
            _current.ceiling,
        )
    else:
        logger.info("memory budget %s", _current.describe())
    return _current


def current() -> Budget | None:
    return _current


def ceiling() -> int | None:
    """How many sessions the memory budget allows, or None when there is no budget."""
    return None if _current is None else _current.ceiling


def set_for_tests(budget: Budget | None) -> None:
    global _current
    _current = budget
