"""Repository identity scoped to one asynchronous poll or session operation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_target: ContextVar[dict[str, Any] | None] = ContextVar("agent_repository", default=None)


def current() -> dict[str, Any] | None:
    return _target.get()


def slug(fallback: str) -> str:
    target = current()
    return str(target["repo_slug"]) if target and target.get("repo_slug") else fallback


@contextmanager
def scope(target: dict[str, Any] | None):
    token = _target.set(target)
    try:
        yield
    finally:
        _target.reset(token)


def local_ref(reference: str) -> str:
    """Remove a linked repository's namespace from a durable trigger reference."""
    return reference.rsplit(":", 1)[-1]
