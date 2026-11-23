"""How many sessions the host's memory can hold at once.

Every session container, and every helper container a session runs in its
place, may use up to ``LOGOS_AGENT_SESSION_MEMORY_MB``. A paused session
keeps its memory too. The admission signal of the runner is spare *serving*
capacity, which says nothing about the host the containers run on, so the
parallel ceiling alone can promise sessions far more memory than the host
has: ten sessions of 4 GB on a host with 11 GB. When they all build at once,
the kernel kills processes of the host, not only of the sessions.

The runner therefore admits sessions against a memory budget: the memory all
sessions together may claim. ``LOGOS_AGENT_MEMORY_BUDGET_MB`` sets it
explicitly; otherwise it is ``LOGOS_AGENT_HOST_MEMORY_SHARE`` of the memory
the Docker host reports, because the runner usually shares its host with the
database and the services of Logos.

An operator sets the memory of one session; the parallel ceiling follows
from it (the budget divided by that memory, never above
``LOGOS_AGENT_MAX_PARALLEL_SESSIONS``) and can only be lowered from there.
Each session keeps the memory it was started with, so admission also counts
what the live sessions actually reserve: a change of the per-session memory
never lets the sessions together exceed the budget.
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
    """The memory all sessions together may claim."""

    budget_mb: int
    host_mb: int | None
    source: str

    def ceiling(self, session_mb: int) -> int:
        """How many sessions of ``session_mb`` the budget holds."""
        if session_mb <= 0:
            return 0
        return max(0, self.budget_mb // session_mb)

    def describe(self, session_mb: int | None = None) -> str:
        host = f" of {self.host_mb} MiB on the host" if self.host_mb is not None else ""
        text = f"{self.budget_mb} MiB{host} ({self.source})"
        if session_mb is not None:
            text += f" holds {self.ceiling(session_mb)} session(s) of {session_mb} MiB"
        return text

    def admission_block(self, *, reserved_mb: int, session_mb: int) -> str:
        """Why one more session of ``session_mb`` does not fit, or an empty string."""
        if reserved_mb + session_mb <= self.budget_mb:
            return ""
        return (
            f"the memory budget is used: {reserved_mb} of {self.budget_mb} MiB are reserved, "
            f"a new session needs {session_mb} MiB"
        )


_current: Budget | None = None


def compute(host_mb: int | None) -> Budget | None:
    """The budget from the settings and the host's memory, or None when neither gives one."""
    if settings.memory_budget_mb > 0:
        return Budget(settings.memory_budget_mb, host_mb, "LOGOS_AGENT_MEMORY_BUDGET_MB")
    if host_mb is None or host_mb <= 0:
        return None
    share = min(max(settings.host_memory_share, 0.0), 1.0)
    return Budget(int(host_mb * share), host_mb, f"{share:.0%} of the host")


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
    else:
        logger.info("memory budget %s", _current.describe(settings.session_memory_mb))
    return _current


def current() -> Budget | None:
    return _current


def ceiling(session_mb: int) -> int | None:
    """How many sessions of ``session_mb`` the budget allows, or None when there is no budget."""
    return None if _current is None else _current.ceiling(session_mb)


def set_for_tests(budget: Budget | None) -> None:
    global _current
    _current = budget
