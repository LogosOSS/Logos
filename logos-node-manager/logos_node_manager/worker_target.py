"""The worker target: replace the container of a GPU worker, drain its node first, return to the old one on a failure.

In the ``draining`` phase the manager asks the orchestrator to cordon the node, so that it gets no new requests, and
waits until the requests that run have finished. Only one node at a time gets the cordon: another node waits for its
turn. The cordon is held until the deployment ends and then given back, whatever the outcome. Without an orchestrator to
ask, or with the drain switched off, the phase passes at once and the container is replaced as before.

Images are removed when the deployment has ended and not earlier: until then a rollback may still need them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from logos_node_manager.docker_ops import Docker, split_image_ref
from logos_node_manager.errors import DockerError
from logos_node_manager.orchestrator import CordonReply, OrchestratorClient, OrchestratorUnavailable
from logos_node_manager.rollback import restore_previous
from logos_node_manager.state import PREVIOUS_TAG, Deployment, FailureKind, Outcome, Phase
from logos_node_manager.targets import Budget, short_id
from logos_node_manager.verify import VerifyResult, verify_container

if TYPE_CHECKING:
    from logos_node_manager.deployer import Deployer

logger = logging.getLogger(__name__)

# The saved log of a failed container is limited, so the record stays small.
MAX_LOG_CHARS = 6000


class WorkerTarget:
    can_roll_back = True

    def __init__(self, deployer: "Deployer", docker: Docker) -> None:
        self.d = deployer
        self.docker = docker

    # ------------------------------------------------------------------ Steps

    def begin(self, deployment: Deployment) -> None:
        """Note what runs now."""
        current = self.docker.container()
        if current is not None:
            deployment.previous_container_id = current.id
            deployment.previous_running = current.running
            deployment.previous_image_id = current.image_id
            deployment.previous_image_ref = current.image_ref
            note = f"running {current.image_ref} ({short_id(current.image_id)}); deploying tag {deployment.tag}"
        else:
            note = f"no worker container yet; deploying tag {deployment.tag}"
        self.d.advance(deployment, Phase.PULLING, note)

    def _keep_previous(self, deployment: Deployment) -> None:
        """Give the image of the running container the name ``<image>:previous``, just before its container goes.

        A pull can move the tag of that image to the new one. Without a name of its own the old image would be a plain
        unnamed image once its container is gone, and ``docker image prune`` takes those. The name moves only here, so
        a deployment that ends before the swap leaves every name as it was.
        """
        previous = deployment.previous_image_id
        ref = deployment.previous_image_ref or ""
        if not previous or previous == deployment.target_image_id:
            # There was no container, or the container keeps its image.
            return
        if not ref or ref.startswith("sha256:"):
            # A container made from a bare image ID has no name to derive "previous" from.
            return
        name, _ = split_image_ref(ref)
        kept = f"{name}:{PREVIOUS_TAG}"
        held = self.docker.image_id(kept)
        if held == previous:
            # An earlier run of this step already moved the name.
            return
        if held:
            # The image that loses the name is removed when the deployment ends. The record is saved before the name
            # moves, so that a stop in between does not lose track of it.
            deployment.stale_image_id = held
            self.d.store.save(deployment)
        self.docker.tag(previous, kept)

    def pull(self, deployment: Deployment) -> None:
        result = self.docker.pull(deployment.tag)
        if not result.ok:
            self.d.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.PULL_FAILED,
                f"docker compose pull failed: {result.tail()}. The running container was not touched.",
            )
            return
        try:
            reference = self.docker.image_ref(deployment.tag)
            image_id = self.docker.image_id(reference)
        except DockerError as exc:
            self.d.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.PULL_FAILED,
                f"{exc}. The running container was not touched.",
            )
            return
        if image_id is None:
            self.d.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.PULL_FAILED,
                f"image {reference} is not on this host after the pull. The running container was not touched.",
            )
            return
        deployment.target_image_ref = reference
        deployment.target_image_id = image_id
        self.d.advance(deployment, Phase.DRAINING, f"pulled {reference} ({short_id(image_id)})")

    def drain(self, deployment: Deployment) -> None:
        """Cordon the node at the orchestrator and wait until the requests that run on it are done.

        The cordon is held until the deployment ends. If the orchestrator cannot be asked before it granted the
        cordon, the deployment goes on without a drain: an orchestrator problem must not stop the deployments that may
        repair it. Once the cordon is granted, an orchestrator that stops answering ends the deployment instead,
        because the requests that still run must not be cut.
        """
        if not self.d.settings.drain_enabled:
            self.d.advance(deployment, Phase.SWAPPING)
            return
        client = self.d.orchestrator
        if client is None:
            self.d.advance(
                deployment, Phase.SWAPPING, "draining skipped: the manager has no orchestrator address or node key"
            )
            return
        if self._nothing_to_replace(deployment):
            # No request has to be protected from a swap that does not happen: the node keeps serving.
            self._finish_unchanged(deployment)
            return
        self._wait_for_drain(deployment, client)

    def _nothing_to_replace(self, deployment: Deployment) -> bool:
        """Whether Compose would leave the running container alone, so that there is nothing to drain for.

        That holds when the container runs, runs the pulled image, and was made from the configuration that Compose
        would make it from now. The image alone is not enough: Compose also recreates the container when a variable of
        the ``.env`` file, the compose file or the image name changed. Whatever cannot be read counts as "something may
        change", and the node drains as it always did.
        """
        try:
            current = self.docker.container()
            expected = self.docker.config_hash(deployment.tag)
        except DockerError:
            return False
        return (
            current is not None
            and current.running
            and current.image_id == deployment.target_image_id
            and current.config_hash is not None
            and current.config_hash == expected
        )

    def _finish_unchanged(self, deployment: Deployment) -> None:
        self.d.finish(
            deployment, Outcome.UNCHANGED, message=f"the container already runs {deployment.target_image_ref}"
        )

    def _wait_for_drain(self, deployment: Deployment, client: OrchestratorClient) -> None:
        """Ask for the cordon until the node has drained, and end the phase.

        The record always leaves this phase: it goes on to the swap, because the node has drained or because the drain
        was skipped, or the deployment ends as failed.

        Every call asks for the cordon again, which the orchestrator answers with the state of the node. That is also
        what renews the cordon, and what takes it again if the orchestrator forgot it, for example after a restart.
        The limits count from times saved in the record, so a restart of the manager does not extend them.
        """
        d = self.d
        settings = d.settings
        d.reset_progress()
        if deployment.drain_requested_at is None:
            deployment.drain_requested_at = d.now()
            d.store.save(deployment)
        turn = Budget(d, settings.turn_timeout_s, deployment.drain_requested_at)
        drain = Budget(d, settings.drain_timeout_s, deployment.cordoned_at) if deployment.cordoned_at else None
        while True:
            d.check_stop()
            try:
                reply = d.ask(lambda: client.cordon(deployment.id))
            except OrchestratorUnavailable as exc:
                if deployment.cordoned_at is None or drain is None:
                    # No cordon is held, so there is nothing to protect: go on without a drain.
                    logger.warning("Deployment %s: draining skipped: %s", deployment.id, exc)
                    d.advance(deployment, Phase.SWAPPING, f"draining skipped: {exc}")
                    return
                # The cordon is held and requests may still run. Without an answer the manager cannot know they are
                # done, so it never replaces the container: it asks again until the drain limit, then gives up.
                if exc.retryable and not drain.exhausted():
                    d.progress(deployment, f"the orchestrator does not answer ({exc}); asking again")
                    continue
                d.finish(
                    deployment,
                    Outcome.FAILED,
                    FailureKind.DRAIN_UNKNOWN,
                    f"the orchestrator stopped answering after the cordon ({exc}), so the node may still have "
                    "requests running. The running container was not touched.",
                )
                return
            if not reply.granted:
                if deployment.cordoned_at is not None:
                    # The orchestrator forgot the cordon and another node holds it now.
                    d.event(deployment, "the cordon was lost; waiting for the turn of the node again")
                    deployment.cordoned_at = None
                    deployment.drain_requested_at = d.now()
                    drain = None
                    turn = Budget(d, settings.turn_timeout_s, deployment.drain_requested_at)
                    d.store.save(deployment)
                why = self._why_denied(reply)
                if turn.exhausted():
                    d.finish(
                        deployment,
                        Outcome.FAILED,
                        FailureKind.DRAIN_BLOCKED,
                        f"the node did not get its turn within {int(turn.limit_s)} s: {why}. "
                        "The running container was not touched.",
                    )
                    return
                d.progress(deployment, f"waiting for its turn: {why}")
                d.pause()
                continue

            if deployment.cordoned_at is None:
                deployment.cordoned_at = d.now()
                deployment.drained_requests = reply.in_flight
                deployment.sole_replica_models = list(reply.sole_replica_models)
                drain = Budget(d, settings.drain_timeout_s, deployment.cordoned_at)
                d.event(deployment, self._cordon_note(reply))
                d.store.save(deployment)

            if reply.drained:
                deployment.drained_at = d.now()
                # The worker session that goes away with the old container. The new container shows up as another one.
                deployment.session_before = reply.session_id
                d.event(deployment, f"the node has drained after {int(drain.used())} s")
                d.advance(deployment, Phase.SWAPPING, "the node has drained; replacing the container")
                return
            if drain.exhausted():
                d.finish(
                    deployment,
                    Outcome.FAILED,
                    FailureKind.DRAIN_TIMEOUT,
                    f"the node did not drain within {int(drain.limit_s)} s: {self._in_flight_text(reply)} "
                    "still running. The cordon was lifted and the running container was not touched.",
                )
                return
            d.progress(deployment, f"waiting for the node to drain: {self._in_flight_text(reply)} running")
            d.pause()

    @staticmethod
    def _why_denied(reply: CordonReply) -> str:
        if reply.reason == "another_node_in_maintenance":
            return f"{reply.holder or 'another node'} is being replaced"
        if reply.reason == "calibrating":
            return "the node runs a calibration session"
        if reply.reason == "calibration_claimed":
            return "a calibration claim of the node is still open"
        if reply.reason == "benchmark_reserved":
            return "a benchmark holds lanes of the node"
        if reply.reason == "maintenance_state_unknown":
            return "the orchestrator could not read the maintenance state of the node"
        return reply.reason or "the orchestrator did not grant the cordon"

    @staticmethod
    def _in_flight_text(reply: CordonReply) -> str:
        # Not the sum of the two counts: a request that runs on the worker is in both. See ``CordonReply.in_flight``.
        return (
            f"{reply.in_flight} request(s) ({reply.in_flight_orchestrator} tracked by the orchestrator, "
            f"{reply.in_flight_worker} on the worker)"
        )

    @staticmethod
    def _cordon_note(reply: CordonReply) -> str:
        if reply.sole_replica_models:
            return (
                "cordoned the node at the orchestrator; no other node serves "
                f"{', '.join(reply.sole_replica_models)}, so requests for them wait until this node is back"
            )
        return "cordoned the node at the orchestrator"

    def before_finish(self, deployment: Deployment) -> None:
        """Give the cordon back. Every ending passes here; a deployment that never held the cordon does nothing."""
        client = self.d.orchestrator
        if client is None or deployment.cordoned_at is None or deployment.cordon_released_at is not None:
            return
        try:
            self.d.ask(lambda: client.release(deployment.id), stoppable=False)
        except OrchestratorUnavailable as exc:
            # The orchestrator lets a cordon go when the manager stops renewing it.
            logger.warning("Deployment %s: the cordon could not be given back (%s); it expires", deployment.id, exc)
        deployment.cordon_released_at = self.d.now()

    def swap(self, deployment: Deployment) -> None:
        current = self.docker.container()

        # A step that is repeated after a stop: the container was already replaced before the record was saved.
        if (
            current is not None
            and deployment.previous_container_id
            and current.id != deployment.previous_container_id
            and current.image_id == deployment.target_image_id
        ):
            deployment.new_container_id = current.id
            self.d.advance(deployment, Phase.VERIFYING, "the container was already replaced")
            return

        self._keep_previous(deployment)
        result = self.docker.up(deployment.tag)
        after = self.docker.container()

        if not result.ok:
            if after is not None and after.id == deployment.previous_container_id and after.running:
                self.d.finish(
                    deployment,
                    Outcome.FAILED,
                    FailureKind.SWAP_FAILED,
                    f"docker compose up failed: {result.tail()}. The previous container is still running.",
                )
                return
            deployment.failure_logs = self.capture_logs()
            self.fail_over(deployment, FailureKind.SWAP_FAILED, f"docker compose up failed: {result.tail()}")
            return

        if after is None:
            self.fail_over(deployment, FailureKind.SWAP_FAILED, "there is no worker container after docker compose up")
            return

        # Compose found nothing to change. Only a container that already ran counts as unchanged: a stopped one that
        # Compose just started still has to be checked.
        if (
            after.id == deployment.previous_container_id
            and after.image_id == deployment.target_image_id
            and deployment.previous_running
        ):
            self._finish_unchanged(deployment)
            return

        deployment.new_container_id = after.id
        self.d.advance(deployment, Phase.VERIFYING, f"started container {after.id[:12]}")

    def verify(self, deployment: Deployment) -> None:
        d = self.d
        result = verify_container(
            self.docker,
            d.settings,
            expected_image_id=deployment.target_image_id,
            timeout_s=d.settings.verify_timeout_s,
            clock=d.clock,
            sleep=d.sleep,
            should_stop=d.should_stop,
        )
        if result.ok:
            result = self._await_reconnect(deployment)
        if result.ok:
            self._succeed(deployment)
            return
        deployment.failure_logs = self.capture_logs()
        self.fail_over(deployment, FailureKind.VERIFY_FAILED, result.reason)

    def _await_reconnect(self, deployment: Deployment) -> VerifyResult:
        """Wait until the orchestrator sees the new worker: connected through a new session, with its first status.

        Docker cannot show this. A worker that starts and answers on its port but cannot register at the orchestrator
        would otherwise count as a success. Only a deployment that drained the node checks it, because only then the
        manager knows the session of the old worker. If the orchestrator cannot be asked, the check is skipped: not
        seeing the node is no proof that the new container is bad.
        """
        d = self.d
        client = d.orchestrator
        if client is None or deployment.cordoned_at is None or deployment.cordon_released_at is not None:
            return VerifyResult(True)
        started = d.clock()
        while True:
            d.check_stop()
            try:
                node = d.ask(lambda: client.status(deployment.id))
            except OrchestratorUnavailable as exc:
                logger.warning("Deployment %s: the connection of the worker was not checked: %s", deployment.id, exc)
                d.event(deployment, f"the connection of the worker to the orchestrator was not checked: {exc}")
                return VerifyResult(True)
            if node.online and node.first_status_received and node.session_id != deployment.session_before:
                d.event(deployment, "the worker is connected to the orchestrator")
                return VerifyResult(True)
            if d.clock() - started >= d.settings.reconnect_timeout_s:
                return VerifyResult(
                    False,
                    f"the worker did not connect to the orchestrator within {int(d.settings.reconnect_timeout_s)} s",
                )
            d.pause()

    def _succeed(self, deployment: Deployment) -> None:
        self.d.finish(deployment, Outcome.SUCCEEDED, message=f"now running {deployment.target_image_ref}")

    def fail_over(self, deployment: Deployment, kind: FailureKind, reason: str) -> None:
        """The new container is bad: restore the previous version, or give up if there is none."""
        deployment.failure = kind
        deployment.message = reason
        if deployment.previous_image_id:
            self.d.advance(deployment, Phase.ROLLING_BACK, f"{reason}; restoring {deployment.previous_image_ref}")
        else:
            self.d.finish(deployment, Outcome.FAILED, kind, f"{reason}; there is no previous version to restore")

    def roll_back(self, deployment: Deployment) -> None:
        d = self.d
        result = restore_previous(
            self.docker,
            d.settings,
            deployment,
            clock=d.clock,
            sleep=d.sleep,
            should_stop=d.should_stop,
        )
        if result.ok:
            d.finish(
                deployment,
                Outcome.ROLLED_BACK,
                deployment.failure,
                f"{deployment.message}. Rolled back to {deployment.previous_image_ref} "
                f"({short_id(deployment.previous_image_id)}).",
            )
        else:
            d.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.ROLLBACK_FAILED,
                f"{deployment.message}. The rollback failed too: {result.reason}.",
            )

    def capture_logs(self) -> str:
        """The end of the container's log. Must run before a rollback removes the container."""
        try:
            return self.docker.container_logs(40)[-MAX_LOG_CHARS:]
        except Exception:  # noqa: BLE001 - the log is a bonus; it must never hide the real failure
            logger.warning("The log of the failed container could not be read", exc_info=True)
            return ""

    # ------------------------------------------------------------------ Clean-up

    def clean_up(self, deployment: Deployment) -> None:
        """Remove the images that this deployment leaves without a purpose.

        These are the image that the name ``previous`` pointed to before (now two versions old) and the image that was
        pulled but does not run. An image that a container uses, or that is the previous version, stays. After a failed
        rollback every image stays, because a person may need them to repair the host.

        The record is already saved when this runs. A problem here is logged and does not change the result.
        """
        if deployment.failure is FailureKind.ROLLBACK_FAILED:
            logger.info("Deployment %s: the images stay on the host because the rollback failed", deployment.id)
            return
        removed = []
        try:
            current = self.docker.container()
            needed = {deployment.previous_image_id, current.image_id if current else None}
            for image_id in dict.fromkeys((deployment.stale_image_id, deployment.target_image_id)):
                if image_id and image_id not in needed and self.docker.remove_image(image_id):
                    removed.append(short_id(image_id))
        except Exception:  # noqa: BLE001 - the deployment is over; a clean-up problem must not change its result
            logger.warning(
                "Deployment %s: the clean-up failed; images may stay on the host", deployment.id, exc_info=True
            )
            return
        logger.info(
            "Deployment %s: clean-up finished, %s",
            deployment.id,
            f"removed {', '.join(removed)}" if removed else "nothing removed",
        )
