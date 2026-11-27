"""Private disk-backed temporary storage, with shared-disk pressure monitoring."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import AsyncIterator, Callable, Coroutine

from . import docker_engine
from .config import settings

ROOT = Path("/var/lib/logos-agent/verification-scratch")
MARKER = ".logos-verifier-scratch"
GIB = 1 << 30
LABEL = "logos.agent.verifier-scratch"
CHECK_INTERVAL_S = 0.25
MIN_FREE_INODES = 16_384
logger = logging.getLogger("logos.agent.verifier_scratch")


@dataclass(frozen=True)
class Scratch:
    volume: str
    slot: str
    marker: str
    min_free_bytes: int

    def check_pressure(self) -> None:
        free_bytes, free_inodes = _available()
        if free_bytes <= self.min_free_bytes or free_inodes <= MIN_FREE_INODES:
            raise RuntimeError("verification scratch disk pressure limit reached; verification aborted")

    async def monitor(self, run: Callable[[], Coroutine[None, None, int | None]]) -> int | None:
        # Check throughout Docker start/wait, not after a successful exit only.
        # An exception unwinds the helper, force-removing it before slot cleanup.
        await asyncio.to_thread(self.check_pressure)
        task = asyncio.create_task(run())
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=CHECK_INTERVAL_S)
                await asyncio.to_thread(self.check_pressure)
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _available() -> tuple[int, int]:
    stats = os.statvfs(ROOT)
    return stats.f_bavail * stats.f_frsize, stats.f_favail


def _validate_mount(root: Path) -> None:
    """Allow an actual writable disk-backed mount, not tmpfs or the root overlay."""
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError("verification scratch mount must be a root-owned directory not writable by group/other")
    if root.resolve() != root:
        raise RuntimeError("verification scratch must not use a symlink")
    dev = metadata.st_dev
    dev_id = f"{os.major(dev)}:{os.minor(dev)}"
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount, _, filesystem = line.partition(" - ")
        parts = mount.split()
        if len(parts) < 6:
            continue
        if parts[4].startswith(f"{root}/"):
            raise RuntimeError("verification scratch cannot contain nested mounts")
        if parts[4] == str(root):
            mounts.append((parts, filesystem.split()))
    if len(mounts) != 1:
        raise RuntimeError("verification scratch must be exactly one mounted Docker volume")
    parts, fs = mounts[0]
    mount_options = set(parts[5].split(","))
    if (
        parts[2] != dev_id
        or len(fs) < 3
        or fs[0] not in {"ext4", "xfs"}
        or "rw" not in mount_options
        or "rw" not in fs[2].split(",")
        or "noexec" in mount_options
    ):
        raise RuntimeError("verification scratch requires a writable ext4 or XFS Docker volume")


async def _validate_daemon_volume(volume: str) -> Path:
    # Resolve this runner's exact ordinary named volume through the Docker
    # daemon. Never pass arbitrary host directories to the verifier.
    identity = socket.gethostname()
    if not re.fullmatch(r"[0-9a-f]{12,64}", identity):
        raise RuntimeError("verification scratch requires the runner's Docker-assigned hostname")
    info = await docker_engine.inspect_volume(volume)
    mountpoint = info.get("Mountpoint")
    if (
        info.get("Name") != volume
        or info.get("Driver") != "local"
        or info.get("Options") not in (None, {})
        or not isinstance(mountpoint, str)
        or not _safe_absolute_path(mountpoint)
    ):
        raise RuntimeError("verification scratch requires a plain local Docker volume (no driver options)")
    runner = await docker_engine.inspect_container(identity)
    if not runner or not runner.get("Id", "").startswith(identity):
        raise RuntimeError("cannot determine the Docker host mapping for the verification scratch mount")
    mounts = [mount for mount in runner.get("Mounts", ()) if mount.get("Destination") == str(ROOT)]
    expected = {"Type": "volume", "Name": volume, "Source": mountpoint, "RW": True}
    if len(mounts) != 1 or any(mounts[0].get(key) != value for key, value in expected.items()):
        raise RuntimeError("verification scratch does not match the runner's read/write Docker volume mount")
    return Path(mountpoint)


async def _wipe(path: Path, lock_fd: int) -> None:
    # A cancelled coroutine can close its descriptor while the worker still
    # deletes files. Its duplicate holds the same lock until deletion ends.
    fd = os.dup(lock_fd)

    def remove() -> None:
        try:
            shutil.rmtree(path)
        finally:
            os.close(fd)

    await asyncio.shield(asyncio.get_running_loop().run_in_executor(None, remove))


async def _wipe_stale(slots: list[Path], host_root: Path, lock_fd: int) -> None:
    if not slots:
        return
    sources = {str(host_root / slot.name) for slot in slots}
    # A killed runner releases its lock even if the Docker child outlived it.
    # Check Docker, not just a PID/lock file, before touching the old data.
    # Startup reconciliation normally removes the orphan before we get here.
    for container in await docker_engine.list_managed_containers():
        if (container.get("Labels") or {}).get(LABEL) == "true":
            raise RuntimeError("previous verification scratch is still assigned to a Docker container")
        for mount in container.get("Mounts") or ():
            if mount.get("Source") in sources:
                raise RuntimeError("previous verification scratch is still mounted by a Docker container")
    for slot in slots:
        await _wipe(slot, lock_fd)


def _safe_absolute_path(value: str) -> bool:
    path = Path(value)
    return (
        path.is_absolute()
        and path != Path("/")
        and str(path) == value
        and ".." not in path.parts
        and not value.startswith("//")
        and not any(char in value for char in "\0\n\r")
    )


def _parse_limit(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        raise RuntimeError("verification scratch limits must be positive integers in GiB (at most 9999)")
    return int(value) * GIB


async def validate_configuration(volume: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", volume):
        raise RuntimeError("verification scratch requires the name of its local Docker volume")
    host = await _validate_daemon_volume(volume)
    await asyncio.to_thread(_validate_mount, ROOT)
    return host


def _open_lock_file() -> int:
    fd = os.open(ROOT / ".lock", os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        lock = os.fstat(fd)
        if not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1 or lock.st_uid != 0 or lock.st_mode & 0o077:
            raise RuntimeError("verification scratch lock must be a private runner-owned file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _close_opened_lock(task: asyncio.Task[int]) -> None:
    if not task.cancelled() and task.exception() is None:
        os.close(task.result())


async def _open_lock() -> int:
    # A disk stalled on open must not block other runner traffic. Keep the
    # task alive on cancellation so the eventual descriptor is closed.
    task = asyncio.create_task(asyncio.to_thread(_open_lock_file))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_close_opened_lock)
        raise


@asynccontextmanager
async def reserve(volume: str, max_gib: str, min_free_gib: str, *, timeout_s: float) -> AsyncIterator[Scratch]:
    budget = _parse_limit(max_gib)
    floor = _parse_limit(min_free_gib)
    host = await validate_configuration(volume)
    # The parent and lock are runner-only. The verifier receives a newly
    # created private directory, whose marker is checked before build code.
    lock_fd = await _open_lock()
    try:
        end = monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                delay = end - monotonic()
                if delay <= 0:
                    raise RuntimeError("verification scratch slot was still busy after waiting for the helper timeout")
                await asyncio.sleep(min(delay, 1))
        slots = []
        for entry in ROOT.iterdir():
            if entry.name in {".lock", "lost+found"}:
                continue
            if not docker_engine.VERIFIER_SLOT_PATTERN.fullmatch(entry.name) or not stat.S_ISDIR(entry.lstat().st_mode):
                raise RuntimeError("verification scratch must contain only runner files at the filesystem root")
            slots.append(entry)
        await _wipe_stale(slots, host, lock_fd)
        free_bytes, free_inodes = await asyncio.to_thread(_available)
        if free_bytes <= floor + GIB or free_inodes <= MIN_FREE_INODES:
            raise RuntimeError("insufficient free space or inodes on the verification scratch filesystem")
        # Other host writers count toward the limit as well. This is a
        # conservative pressure guard; it cannot enforce per-volume quotas.
        min_free_bytes = max(floor, free_bytes - budget)
        # Never reuse a slot after an uncertain Docker create, even if the
        # unstarted container did not appear in the cleanup listing yet.
        slot = ROOT / f"verifier-{secrets.token_hex(16)}"
        slot.mkdir(mode=0o700)
        try:
            marker = secrets.token_hex(32)
            (slot / MARKER).write_text(marker, encoding="ascii")
            (slot / MARKER).chmod(0o444)
            os.chown(slot, settings.session_uid, -1)
            yield Scratch(volume, slot.name, marker, min_free_bytes)
        finally:
            # A removed container is absent (including in the all-states
            # list). If removal couldn't be confirmed, keep the slot.
            try:
                await _wipe_stale([slot], host, lock_fd)
            except (OSError, RuntimeError) as exc:
                logger.warning("retained verification scratch: %s", exc)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    try:
        asyncio.run(validate_configuration(settings.verification_scratch_volume))
    except (OSError, RuntimeError) as exc:
        sys.exit(f"Verification scratch: {exc}")
    print("Verification scratch filesystem validated.")
