"""Test doubles and builders for the self-update tests.

``FakeHelperEngine`` answers the docker commands that start and look at the helper container. ``Wall`` is the wall clock
of the manager, which a test can move. The builders make the records that the manager and the helper leave behind.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import NEW, OLD, REPO

from logos_node_manager.runner import CommandResult
from logos_node_manager.self_update import HELPER_NAME, SelfUpdate, SelfUpdateStore, UpdatePhase
from logos_node_manager.state import Outcome

WHEN = "2026-01-01T00:00:00.000000+00:00"


class Wall:
    """The wall clock of the manager, as the text that records keep. It starts at ``WHEN``."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.moment.isoformat(timespec="microseconds")

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class StubRegistry:
    """Stands for the registry login: counts the logins and fails with the given reason, if there is one."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.logins = 0

    def login(self) -> str | None:
        self.logins += 1
        return self.problem


class FakeHelperEngine:
    """The docker commands that start and look at the helper container. Every field is a knob a test can turn."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.exists = False
        self.running = False
        self.exit_code = 0
        self.run_error: str | None = None
        self.inspect_error: str | None = None
        self.log = ""

    def run(self, argv, *, timeout_s, env=None, stdin=None) -> CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        verb = command[1]
        if verb == "run":
            if self.run_error:
                return CommandResult(command, 1, stderr=self.run_error)
            if self.exists:
                # A real engine refuses a second container with the same name.
                return CommandResult(command, 1, stderr="Conflict. The container name is already in use")
            self.exists = self.running = True
            return CommandResult(command, 0, stdout="helper-container-id\n")
        if verb == "inspect":
            if self.inspect_error:
                return CommandResult(command, 1, stderr=self.inspect_error)
            if not self.exists:
                return CommandResult(command, 1, stderr=f"Error response from daemon: No such container: {HELPER_NAME}")
            state = {
                "Running": self.running,
                "Status": "running" if self.running else "exited",
                "ExitCode": self.exit_code,
            }
            return CommandResult(command, 0, stdout=json.dumps([{"State": state}]))
        if verb == "logs":
            return CommandResult(command, 0, stdout=self.log)
        if verb == "rm":
            self.exists = self.running = False
            return CommandResult(command, 0)
        raise AssertionError(f"the manager ran a command that this fake does not know: {command}")

    def verbs(self) -> list[str]:
        return [command[1] for command in self.calls]

    def started(self) -> tuple[str, ...]:
        """The command that started the helper."""
        return next(command for command in self.calls if command[1] == "run")


def request(store: SelfUpdateStore, tag: str = "v2", update_id: str = "u1") -> SelfUpdate:
    """A request that waits in the queue."""
    return store.submit(SelfUpdate(id=update_id, tag=tag, requested_at=WHEN))


def handed_over(
    store: SelfUpdateStore, phase: UpdatePhase = UpdatePhase.HANDED_OVER, update_id: str = "u1", **fields
) -> SelfUpdate:
    """A request that the manager handed to the helper: the manager runs OLD and the image to install is NEW."""
    values = {
        "id": update_id,
        "tag": "v2",
        "requested_at": WHEN,
        "phase": phase,
        "handed_over_at": WHEN,
        "previous_container_id": "c-old",
        "previous_image_id": OLD,
        "previous_image_ref": f"{REPO}:latest",
        "target_image_id": NEW,
        "target_image_ref": f"{REPO}:v2",
        **fields,
    }
    record = SelfUpdate(**values)
    store.save(record)
    return record


def ended(store: SelfUpdateStore, outcome: Outcome, update_id: str = "u1", **fields) -> SelfUpdate:
    """A request that is finished, whose images have not been cleaned up yet."""
    record = handed_over(store, UpdatePhase.DONE, update_id, **fields)
    record.outcome = outcome
    record.finished_at = WHEN
    store.save(record)
    return record
