"""Bounded reads of untrusted session artifacts."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from .config import settings

logger = logging.getLogger(__name__)


def read_artifact(directory: Path, relative: str | Path, *, limit: int | None = None) -> bytes:
    """Read a regular file beneath a session directory, without following links.

    Open each directory relative to its pinned parent descriptor so a rename
    or symlink swap cannot redirect the read. Missing files retain their
    FileNotFoundError; unsafe and oversized files raise OSError.
    """
    limit = settings.artifact_max_bytes if limit is None else limit
    relative = Path(relative)
    descriptors: list[int] = []
    try:
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise OSError("artifact path must stay within its session directory")
        root = os.path.realpath(directory)
        if os.path.commonpath([root, os.path.realpath(os.path.join(root, relative))]) != root:
            raise OSError("artifact path must stay within its session directory")
        if limit < 0:
            raise OSError("artifact size limit must not be negative")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
        descriptors.append(os.open(directory, flags))
        for part in relative.parts[:-1]:
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptors[-1])
        descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("artifact is not a regular file")
        if info.st_size > limit:
            raise OSError(f"artifact exceeds {limit} bytes")
        chunks: list[bytes] = []
        total = 0
        while total < limit:
            chunk = os.read(fd, min(65536, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        # Recheck the descriptor to reject a file that grew during the read
        # without reading even one byte beyond the configured ceiling.
        if os.fstat(fd).st_size > limit:
            raise OSError(f"artifact exceeds {limit} bytes")
        return b"".join(chunks)
    except FileNotFoundError:
        raise
    except OSError as exc:
        logger.warning("unreadable artifact %s/%r: %s", directory, str(relative), exc)
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_artifact_text(directory: Path, relative: str | Path, *, limit: int | None = None) -> str:
    """Decode an artifact, treating invalid UTF-8 as an unreadable file."""
    try:
        return read_artifact(directory, relative, limit=limit).decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.warning("unreadable UTF-8 artifact %s/%r", directory, str(relative))
        raise OSError("artifact is not valid UTF-8") from exc
