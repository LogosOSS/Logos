"""Private, bounded disk-backed temporary storage for one verifier at a time."""

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
from typing import AsyncIterator

from . import docker_engine
from .config import settings

ROOT = Path("/var/lib/logos-agent/verification-scratch")
SLOT_PATTERN = re.compile(r"verifier-[0-9a-f]{32}")
MARKER = ".logos-verifier-scratch"
GIB = 1 << 30
LABEL = "logos.agent.verifier-scratch"
logger = logging.getLogger("logos.agent.verifier_scratch")


@dataclass(frozen=True)
class Scratch:
    host_path: str
    marker: str


def _disk_device(dev_id: str, sys_blocks: Path = Path("/sys/dev/block")) -> bool:
    """Validate the device and its lower devices, including DM/LVM or RAID."""
    pending = [sys_blocks / dev_id]
    seen = set()
    while pending:
        path = pending.pop().resolve()
        if path in seen:
            continue
        seen.add(path)
        major = (path / "dev").read_text().partition(":")[0]
        if not major.isdecimal() or int(major) in {0, 1, 7} or path.name.startswith("zram") or (path / "loop").exists():
            return False
        # Partitions have the disk's backing storage, which can itself be a
        # mapped device. Unpartitioned devices expose `slaves` directly.
        if (path / "partition").exists():
            pending.append(path.parent)
        elif (path / "slaves").is_dir():
            pending.extend((path / "slaves").iterdir())
    return True


def _validate_mount(root: Path, max_gib: int, *, read_only: bool = False) -> None:
    """Require the root of a reserved, size-limited Linux disk filesystem."""
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError("verification scratch mount must be a root-owned directory not writable by group/other")
    dev = metadata.st_dev
    # A pre-swap read-only probe mounts no other host data. The running
    # agent repeats the check against its actual artifact and state mounts.
    adjacent = (root.parent,) if read_only else (root.parent, settings.artifact_root, settings.state_root)
    if root.resolve() != root or any(dev == os.stat(other).st_dev for other in adjacent):
        raise RuntimeError("verification scratch requires a dedicated filesystem, not a shared directory")
    dev_id = f"{os.major(dev)}:{os.minor(dev)}"
    # mountinfo escapes spaces, tabs, newlines and backslashes. ROOT has none.
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
        raise RuntimeError("verification scratch must mount exactly one dedicated filesystem")
    parts, fs = mounts[0]
    mode = "ro" if read_only else "rw"
    if (
        parts[2] != dev_id
        or parts[3] != "/"
        or len(fs) < 3
        or fs[0] not in {"ext4", "xfs"}
        or fs[1].startswith("/dev/loop")
        or not {mode, "nodev", "nosuid"}.issubset(parts[5].split(","))
        or "rw" not in fs[2].split(",")
        or "noexec" in parts[5].split(",")
        or not _disk_device(dev_id)
    ):
        raise RuntimeError(
            "verification scratch requires a dedicated writable ext4/XFS block filesystem (nodev,nosuid,exec)"
        )
    size = os.statvfs(root)
    total = size.f_blocks * size.f_frsize
    if not 0 < total <= max_gib * GIB:
        raise RuntimeError(f"verification scratch filesystem exceeds its configured {max_gib} GiB limit")
    if size.f_bavail == 0:
        raise RuntimeError("verification scratch filesystem is full")


async def _validate_daemon_bind(volume: str, host_path: str) -> Path:
    # Verify the volume was explicitly provisioned, and that our Compose
    # runner really mounts it, rather than a similarly named ordinary volume.
    identity = socket.gethostname()
    if not re.fullmatch(r"[0-9a-f]{12,64}", identity):
        raise RuntimeError("verification scratch requires the runner's Docker-assigned hostname")
    info = await docker_engine.inspect_volume(volume)
    mountpoint = info.get("Mountpoint")
    if (
        info.get("Name") != volume
        or info.get("Driver") != "local"
        or info.get("Options") != {"device": host_path, "type": "none", "o": "bind"}
        or not isinstance(mountpoint, str)
        or not _safe_absolute_path(mountpoint)
    ):
        raise RuntimeError("verification scratch requires the explicitly provisioned local bind-backed volume")
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


def _parse_limit(max_gib: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]{0,3}", max_gib):
        raise RuntimeError("verification scratch limit must be a positive integer in GiB (at most 9999)")
    return int(max_gib)


async def validate_configuration(volume: str, host_path: str, max_gib: str) -> Path:
    if not volume or not host_path:
        raise RuntimeError(
            "provision dedicated verification storage and set LOGOS_AGENT_VERIFICATION_SCRATCH_VOLUME "
            "and LOGOS_AGENT_VERIFICATION_SCRATCH_HOST_PATH"
        )
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}", volume):
        raise RuntimeError("verification scratch requires the name of its dedicated Docker volume")
    if not _safe_absolute_path(host_path):
        raise RuntimeError("verification scratch requires an absolute, normalized Docker host path")
    limit = _parse_limit(max_gib)
    host = await _validate_daemon_bind(volume, host_path)
    _validate_mount(ROOT, limit)
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
async def reserve(volume: str, host_path: str, max_gib: str, *, timeout_s: float) -> AsyncIterator[Scratch]:
    host = await validate_configuration(volume, host_path, max_gib)
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
            if not SLOT_PATTERN.fullmatch(entry.name) or not stat.S_ISDIR(entry.lstat().st_mode):
                raise RuntimeError("verification scratch must contain only runner files at the filesystem root")
            slots.append(entry)
        await _wipe_stale(slots, host, lock_fd)
        # Never reuse a bind path after an uncertain Docker create, even if
        # its unstarted container did not appear in the cleanup listing yet.
        slot = ROOT / f"verifier-{secrets.token_hex(16)}"
        slot.mkdir(mode=0o700)
        try:
            marker = secrets.token_hex(32)
            (slot / MARKER).write_text(marker, encoding="ascii")
            (slot / MARKER).chmod(0o444)
            os.chown(slot, settings.session_uid, -1)
            yield Scratch(str(host / slot.name), marker)
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
        if sys.argv[1:] == ["--volume-probe"]:
            _validate_mount(ROOT, _parse_limit(settings.verification_scratch_max_gib), read_only=True)
        elif not sys.argv[1:]:
            asyncio.run(
                validate_configuration(
                    settings.verification_scratch_volume,
                    settings.verification_scratch_host_path,
                    settings.verification_scratch_max_gib,
                )
            )
        else:
            raise RuntimeError("unrecognized verification scratch command")
    except (OSError, RuntimeError) as exc:
        sys.exit(f"Verification scratch: {exc}")
    print("Verification scratch filesystem validated.")
