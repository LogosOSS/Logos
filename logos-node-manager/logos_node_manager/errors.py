"""Exceptions shared by the modules of the node manager."""

from __future__ import annotations


class DockerError(RuntimeError):
    """A Docker command failed in a way the deployment cannot work around."""


class StateError(RuntimeError):
    """A deployment record is unreadable, or a request contradicts a record that exists."""


class ComposeError(RuntimeError):
    """The compose file of a tag could not be taken out of its image. The text says why, without any secret."""


class Interrupted(Exception):
    """The daemon was asked to stop.

    The record keeps the phase it had, so the next start continues from there. This is not a failure of the
    deployment.
    """
