"""Process execution behind one seam.

The manager acts on the host only through the ``docker`` command line. Every call goes through a ``Runner``, so the
tests replace the real one with a fake and never need a Docker engine.
"""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# The exit codes that timeout(1) and a shell use for "timed out" and "command not found".
EXIT_TIMED_OUT = 124
EXIT_NOT_FOUND = 127

# A command that ran out of time first gets the request to stop. It is killed if it is still there after this long.
STOP_GRACE_S = 10.0
# What is left of the output is read for at most this long after the kill, in case a process outside the group
# still holds the pipes.
DRAIN_S = 5.0


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 12) -> str:
        """The last lines of the output, for an error message."""
        if self.timed_out:
            return "the command timed out"
        text = "\n".join(part for part in (self.stderr.strip(), self.stdout.strip()) if part)
        return "\n".join(text.splitlines()[-lines:]) or f"exit code {self.returncode}"


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult: ...


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


class SubprocessRunner:
    """Runs the command on this machine.

    The environment of the process is the base. ``set_env`` adds variables for every later command, and ``env`` adds
    some for one command, on top. Only the command line is logged, and only at debug level: it holds file paths and
    image tags, and the environment, which can hold secrets, is never written. A secret goes to a command through
    ``stdin``, never through the command line.

    Every command runs in a process group of its own. ``docker compose`` hands the work to a helper process, so a
    command that ran out of time is stopped together with everything it started: the group gets SIGTERM, and SIGKILL
    after ``grace_s`` seconds. What the Docker engine has begun by then, for example stopping a container, goes on.
    """

    def __init__(self, *, grace_s: float = STOP_GRACE_S) -> None:
        self._grace_s = grace_s
        self._base_env: dict[str, str] = {}

    def set_env(self, name: str, value: str | None) -> None:
        """Add a variable to the environment of every later command. ``None`` takes it away again."""
        if value is None:
            self._base_env.pop(name, None)
        else:
            self._base_env[name] = value

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
    ) -> CommandResult:
        command = tuple(argv)
        logger.debug("run: %s", shlex.join(command))
        extra = {**self._base_env, **(env or {})}
        full_env = {**os.environ, **extra} if extra else None
        stdin_option = {"stdin": subprocess.PIPE} if stdin is not None else {}
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=full_env,
                start_new_session=True,
                **stdin_option,
            )
        except FileNotFoundError:
            return CommandResult(command, EXIT_NOT_FOUND, stderr=f"{command[0]}: command not found")
        with process:
            try:
                stdout, stderr = process.communicate(input=stdin, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "A %s command ran out of time after %s s and is stopped", shlex.join(command[:2]), timeout_s
                )
                stdout, stderr = self._stop(process)
                return CommandResult(command, EXIT_TIMED_OUT, stdout, stderr, timed_out=True)
            except BaseException:
                _signal_group(process, signal.SIGKILL)
                raise
        return CommandResult(command, process.returncode, stdout, stderr)

    def _stop(self, process: subprocess.Popen) -> tuple[str, str]:
        """Stop the whole group, and return what it wrote."""
        _signal_group(process, signal.SIGTERM)
        try:
            out, err = process.communicate(timeout=self._grace_s)
            return _as_text(out), _as_text(err)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(process, signal.SIGKILL)
        try:
            out, err = process.communicate(timeout=DRAIN_S)
            return _as_text(out), _as_text(err)
        except subprocess.TimeoutExpired as exc:
            return _as_text(exc.stdout), _as_text(exc.stderr)


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Send the signal to every process of the group that the command leads."""
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # no process of the group is left
