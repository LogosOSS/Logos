"""Read-only storage telemetry; not a workspace quota or an admission signal."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from . import docker_engine
from .config import settings
from .schemas import ArtifactFilesystem, WorkspaceStorage
from .sessions import cache_volume_for

logger = logging.getLogger(__name__)


class VolumeUsage:
    """Coalesce operator requests; Docker walks local volumes for this reading."""

    def __init__(self, interval: float = 90) -> None:
        self.interval = interval
        self._until = float("-inf")
        self._value: dict[str, int | None] | None = None
        self._lock = asyncio.Lock()

    async def read(self) -> dict[str, int | None] | None:
        async with self._lock:
            if time.monotonic() >= self._until:
                try:
                    async with asyncio.timeout(8):
                        self._value = await docker_engine.volume_usage_bytes()
                except (RuntimeError, OSError, httpx.HTTPError, ValueError) as exc:
                    logger.debug("Docker volume usage is unavailable: %s", type(exc).__name__)
                    self._value = None
                self._until = time.monotonic() + self.interval
            return self._value


_volume_usage = VolumeUsage()


def artifact_filesystem() -> ArtifactFilesystem | None:
    """Read the actual mounted artifact filesystem, not the container root."""
    try:
        fs = os.statvfs(settings.artifact_root)
    except OSError:
        return None
    if fs.f_blocks <= 0 or fs.f_frsize <= 0 or not 0 <= fs.f_bavail <= fs.f_blocks:
        return None
    return ArtifactFilesystem(total_bytes=fs.f_blocks * fs.f_frsize, available_bytes=fs.f_bavail * fs.f_frsize)


async def read(workspaces: list[dict]) -> WorkspaceStorage:
    filesystem = artifact_filesystem()
    usage = await _volume_usage.read() if workspaces else None
    by_workspace = {}
    if usage is not None:
        for row in workspaces:
            checkout = usage.get(row["volume_name"])
            # Older and unlinked workspaces do not have a separate cache volume.
            # If Docker reports a cache but has no measurement for it, the sum
            # is unknown rather than showing only part of the storage.
            cache = usage.get(cache_volume_for(row), 0)
            by_workspace[row["id"]] = checkout + cache if checkout is not None and cache is not None else None
    return WorkspaceStorage(artifact_filesystem=filesystem, workspace_used_bytes=by_workspace)
