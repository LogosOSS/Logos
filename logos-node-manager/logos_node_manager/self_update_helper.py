"""The helper of the self-update: swap the manager, check the new one, and go back if it does not come up.

The manager starts the helper as a container of its own (see ``self_update``) and starts no deployment while it runs.
The helper is the image of the old manager, so the code that supervises the swap is the code that is known to work. It
is not a part of the compose project, so Compose does not stop it when it replaces the manager.

The steps follow the deployment of a worker, with the manager's own service in the place of the worker:

* ``swapping``: the image is on the host already, so Compose recreates the service without a pull. The old image keeps
  the name ``<image>:previous`` first, so that a pull that moved the tag cannot leave it without a name.
* ``verifying``: the new container runs, is healthy and does not restart, for the stability window.
* ``rolling_back``: if the new manager does not come up, the old image gets the name that the service uses again, and
  Compose recreates the service from it, with the compose file that made the old manager.

Every step can be repeated, and the record is saved when the phase changes. A helper that is started again for the same
record, by a person for example, goes on from the recorded phase.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from logos_node_manager.config import Settings
from logos_node_manager.docker_ops import Docker, self_docker, split_image_ref
from logos_node_manager.errors import DockerError
from logos_node_manager.rollback import restore_previous
from logos_node_manager.runner import Runner, SubprocessRunner
from logos_node_manager.self_update import MAX_ATTEMPTS, Failure, Recorder, SelfUpdate, SelfUpdateStore, UpdatePhase
from logos_node_manager.state import PREVIOUS_TAG, Outcome, utc_now
from logos_node_manager.targets import short_id
from logos_node_manager.verify import verify_container

logger = logging.getLogger(__name__)

# The pause after an error of the helper itself, before the step is tried again.
RETRY_PAUSE_S = 5.0


class Swapper(Recorder):
    """The phases of the swap, for one record."""

    def __init__(
        self,
        settings: Settings,
        docker: Docker,
        store: SelfUpdateStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = utc_now,
    ) -> None:
        super().__init__(store, now)
        self.settings = settings
        self.docker = docker
        self.clock = clock
        self.sleep = sleep

    def run(self, record: SelfUpdate) -> SelfUpdate:
        """Take the record to its end. An error of the helper itself is tried again, and after a few it gives up."""
        steps = {
            UpdatePhase.HANDED_OVER: self.begin,
            UpdatePhase.SWAPPING: self.swap,
            UpdatePhase.VERIFYING: self.verify,
            UpdatePhase.ROLLING_BACK: self.roll_back,
        }
        while not record.finished:
            step = steps.get(record.phase)
            if step is None:
                self.finish(
                    record, Outcome.FAILED, Failure.HELPER_FAILED, "the update was not handed over to the helper"
                )
                break
            try:
                step(record)
            except Exception as exc:  # noqa: BLE001 - the helper must give the record an ending
                logger.exception("Self-update %s: the helper hit an unexpected error", record.id)
                record.attempts += 1
                if record.attempts >= MAX_ATTEMPTS:
                    self.give_up(record, exc)
                else:
                    self.store.save(record)
                    self.sleep(RETRY_PAUSE_S)
        return record

    # ------------------------------------------------------------------ Steps

    def begin(self, record: SelfUpdate) -> None:
        self.advance(record, UpdatePhase.SWAPPING, "the helper has the swap")

    def _compose_file(self, name: str) -> Path | None:
        """The compose file named in the record, or None for the compose file of the host."""
        if not name:
            return None
        path = Path(name)
        if not path.is_file():
            raise DockerError(f"the compose file of this update is missing: {path}")
        return path

    def _keep_previous(self, record: SelfUpdate) -> None:
        """Give the image of the running manager the name ``<image>:previous``, just before its container goes."""
        previous = record.previous_image_id
        ref = record.previous_image_ref or ""
        if not previous or previous == record.target_image_id or not ref or ref.startswith("sha256:"):
            return
        name, _ = split_image_ref(ref)
        kept = f"{name}:{PREVIOUS_TAG}"
        if self.docker.image_id(kept) != previous:
            self.docker.tag(previous, kept)

    def swap(self, record: SelfUpdate) -> None:
        docker = self.docker
        docker.use_compose(self._compose_file(record.compose_file))
        current = docker.container()

        # A step that is repeated after a stop: the container was replaced before the record was saved.
        if (
            current is not None
            and record.previous_container_id
            and current.id != record.previous_container_id
            and current.image_id == record.target_image_id
        ):
            record.new_container_id = current.id
            self.advance(record, UpdatePhase.VERIFYING, "the container was already replaced")
            return

        self._keep_previous(record)
        result = docker.up(record.tag, pull_never=True)
        after = docker.container()

        if not result.ok:
            if after is not None and after.id == record.previous_container_id and after.running:
                self.finish(
                    record,
                    Outcome.FAILED,
                    Failure.SWAP_FAILED,
                    f"docker compose up failed: {result.tail()}. The previous manager is still running.",
                )
                return
            self.fail_over(record, Failure.SWAP_FAILED, f"docker compose up failed: {result.tail()}")
            return
        if after is None:
            self.fail_over(record, Failure.SWAP_FAILED, "there is no manager container after docker compose up")
            return
        if after.id == record.previous_container_id:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.SWAP_FAILED,
                "docker compose up did not replace the container of the manager: the service does not name the "
                f"image {record.target_image_ref}. The previous manager is still running.",
            )
            return
        record.new_container_id = after.id
        self.advance(record, UpdatePhase.VERIFYING, f"started container {after.id[:12]}")

    def verify(self, record: SelfUpdate) -> None:
        result = verify_container(
            self.docker,
            self.settings,
            expected_image_id=record.target_image_id,
            timeout_s=self.settings.self_verify_timeout_s,
            clock=self.clock,
            sleep=self.sleep,
        )
        if result.ok:
            self.finish(record, Outcome.SUCCEEDED, message=f"now running {record.target_image_ref}")
            return
        self.fail_over(record, Failure.VERIFY_FAILED, result.reason)

    def fail_over(self, record: SelfUpdate, kind: Failure, reason: str) -> None:
        """The new manager is bad: restore the previous one, or end the record as failed if there is none."""
        record.failure = kind
        record.message = reason
        if record.previous_image_id:
            self.advance(record, UpdatePhase.ROLLING_BACK, f"{reason}; restoring {record.previous_image_ref}")
        else:
            self.finish(record, Outcome.FAILED, kind, f"{reason}; there is no previous version to restore")

    def roll_back(self, record: SelfUpdate) -> None:
        # The previous image goes with the compose file that made it. The file of the new version may ask for things
        # that the old image does not know.
        try:
            previous_file = self._compose_file(record.rollback_compose_file)
        except DockerError:
            previous_file = None
        self.docker.use_compose(previous_file)
        result = restore_previous(
            self.docker, self.settings, record, clock=self.clock, sleep=self.sleep, should_stop=lambda: False
        )
        if result.ok:
            self.finish(
                record,
                Outcome.ROLLED_BACK,
                record.failure,
                f"{record.message}. Rolled back to {record.previous_image_ref} ({short_id(record.previous_image_id)}).",
            )
        else:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.ROLLBACK_FAILED,
                f"{record.message}. The rollback failed too: {result.reason}. Both images stay on the host.",
            )

    def give_up(self, record: SelfUpdate, error: Exception) -> None:
        """End the attempts after the helper itself failed again and again in the phase of the record."""
        reason = f"the helper failed {record.attempts} times: {error}"
        logger.warning("Self-update %s: giving up in phase %s: %s", record.id, record.phase.value, reason)
        if record.phase is UpdatePhase.ROLLING_BACK:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.ROLLBACK_FAILED,
                f"{record.message}. The rollback failed too: {reason}. Both images stay on the host.",
            )
        elif record.phase in (UpdatePhase.SWAPPING, UpdatePhase.VERIFYING):
            self.fail_over(record, Failure.INTERNAL_ERROR, reason)
        else:
            self.finish(record, Outcome.FAILED, Failure.INTERNAL_ERROR, reason)


def run_helper(
    settings: Settings,
    update_id: str,
    *,
    runner: Runner | None = None,
    docker: Docker | None = None,
    store: SelfUpdateStore | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = utc_now,
) -> SelfUpdate | None:
    """Run the swap of the record ``update_id`` to its end. Returns None if there is no such record."""
    store = store or SelfUpdateStore(settings.state_dir)
    record = store.load(update_id)
    if record is None:
        return None
    docker = docker or self_docker(settings, runner or SubprocessRunner())
    return Swapper(settings, docker, store, clock=clock, sleep=sleep, now=now).run(record)
