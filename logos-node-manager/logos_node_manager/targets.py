"""What differs between the hosts a deployment can replace things on.

A deployment moves through the same phases everywhere (see ``Phase``): queued, pulling, draining, swapping, verifying
and, if the new version does not come up, rolling back. What a phase does depends on the host. A worker host replaces
one container, drains its node first and returns to the previous container on a failure (``WorkerTarget``). A core host
replaces the services of the whole Logos compose project and has no previous version to return to (``CoreTarget``).

The deployer keeps what is the same on both: the loop over the phases, the record, the time limits and the reports. A
target does the work of one phase and moves the record on with the helpers of the deployer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from logos_node_manager.deployer import Deployer
    from logos_node_manager.state import Deployment, FailureKind


def short_id(image_id: str | None) -> str:
    """The short form of an image ID, as ``docker images`` prints it."""
    return (image_id or "none").removeprefix("sha256:")[:12]


class Budget:
    """A time limit that survives a restart of the manager.

    The part used before the restart is read from the time saved in the record. The part after it is counted with the
    clock of the deployer.
    """

    def __init__(self, deployer: "Deployer", limit_s: float, started: str | None) -> None:
        self.limit_s = limit_s
        self._clock = deployer.clock
        self._used_before = deployer.seconds_since(started)
        self._begun = self._clock()

    def used(self) -> float:
        return self._used_before + (self._clock() - self._begun)

    def exhausted(self) -> bool:
        return self.used() >= self.limit_s


class Target(Protocol):
    """The work of the phases on one kind of host."""

    # Whether a failed swap or check can end with the previous version running again.
    can_roll_back: bool

    def begin(self, deployment: "Deployment") -> None:
        """Note what runs now. Moves the record to the pull."""

    def pull(self, deployment: "Deployment") -> None:
        """Get the images for the tag onto the host. Moves the record to the drain or ends it as failed."""

    def drain(self, deployment: "Deployment") -> None:
        """Make the host ready to lose its running version. Moves the record to the swap or ends it."""

    def swap(self, deployment: "Deployment") -> None:
        """Start the new version. Moves the record to the check or ends it."""

    def verify(self, deployment: "Deployment") -> None:
        """Check that the new version runs. Ends the record, or moves it to the rollback."""

    def roll_back(self, deployment: "Deployment") -> None:
        """Start the previous version again. Ends the record."""

    def before_finish(self, deployment: "Deployment") -> None:
        """Run before the record says it is over, for example to give back what the deployment held."""

    def capture_logs(self) -> str:
        """The end of the log of the failing part, taken before a rollback removes it. Never raises."""

    def fail_over(self, deployment: "Deployment", kind: "FailureKind", reason: str) -> None:
        """The new version is bad: restore the previous one if there is one, or end the record as failed."""

    def clean_up(self, deployment: "Deployment") -> None:
        """Remove what the ended deployment leaves without a purpose. A problem here never changes the result."""
