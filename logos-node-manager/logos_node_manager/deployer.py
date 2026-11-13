"""The deployment: pull, swap, verify, and roll back if the new version does not come up.

A deployment moves through the phases of ``Phase``. Every step is safe to repeat, and the record is saved when the
phase changes. If the manager stops in the middle, for example because Docker recreates its own container, the next
start picks the record up and works out from the phase and from what Docker shows where to continue.

The deployer owns what is the same on every host: the loop over the phases, the record helpers, the time limits and the
reports. The work of a phase belongs to the target of the host (see ``targets``): ``WorkerTarget`` replaces the
container of a GPU worker, ``CoreTarget`` the services of the Logos compose project.

When a deployment has ended, ``report_pending`` tells the orchestrator how, so that it notifies the Logos admins. This
is separate from the deployment: a report that fails never changes how the deployment ended.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from logos_node_manager.compose_source import ComposeSource
from logos_node_manager.config import Settings
from logos_node_manager.docker_ops import Docker
from logos_node_manager.errors import ComposeError, DockerError, Interrupted
from logos_node_manager.orchestrator import OrchestratorClient, OrchestratorUnavailable
from logos_node_manager.registry import RegistryLogin
from logos_node_manager.state import MAX_EVENTS, Deployment, FailureKind, Outcome, Phase, StateStore, utc_now
from logos_node_manager.targets import Target
from logos_node_manager.worker_target import WorkerTarget

logger = logging.getLogger(__name__)

# A record holds at most MAX_EVENTS events. The wait for the drain adds one at most this often, and only if it changed.
PROGRESS_EVENT_EVERY_S = 30.0
# An ending is reported only while it is this recent: when it is found at the start, and while its report keeps failing.
# This keeps the records from before the manager began to report from being announced late, all at once.
REPORT_MAX_AGE_S = 3600.0
# A report that failed is tried again after this pause, then after twice as long each time, up to the limit.
REPORT_RETRY_FIRST_S = 15.0
REPORT_RETRY_MAX_S = 300.0
# What of the free text of a record goes into a report. The orchestrator accepts more; an inbox item should stay short.
_MAX_REPORT_MESSAGE_CHARS = 1500
_MAX_REPORT_ORIGIN_CHARS = 300

_T = TypeVar("_T")


@dataclass
class _PendingReport:
    """An ending that still has to be reported, and when to try next (on the clock of the deployer)."""

    due_at: float
    failures: int = 0


class Deployer:
    def __init__(
        self,
        settings: Settings,
        docker: Docker | None,
        store: StateStore,
        *,
        orchestrator: OrchestratorClient | None = None,
        target_factory: Callable[["Deployer"], Target] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], str] = utc_now,
        should_stop: Callable[[], bool] = lambda: False,
        registry: RegistryLogin | None = None,
        compose: ComposeSource | None = None,
    ) -> None:
        """``target_factory`` builds the target of the host from this deployer, which the target needs for its helpers.

        Without it the host is a worker host and ``docker`` is its Docker object. ``registry`` logs in to the registry
        before a deployment pulls, and ``compose`` takes the compose file out of the image of the tag. Without them the
        deployment does neither, and the host has to have its login and its compose file by other means.
        """
        self.settings = settings
        self.docker = docker
        self.store = store
        self.orchestrator = orchestrator
        self.clock = clock
        self.sleep = sleep
        self.now = now
        self.should_stop = should_stop
        self.registry = registry
        self.compose = compose
        if target_factory is None:
            if docker is None:
                raise ValueError("a deployer without a target factory needs a Docker object for the worker target")
            self.target: Target = WorkerTarget(self, docker)
        else:
            self.target = target_factory(self)
        # The last line the wait for the drain recorded, and when. Keeps the record from filling with polls.
        self._last_progress: tuple[str, float] | None = None
        # The endings that still have to be reported, oldest first. They come from one look at the records when the
        # manager starts and from every ending after that, so a pass with nothing due reads no record.
        self._pending_reports: dict[str, _PendingReport] = {}
        self._looked_for_unreported = False

    # ------------------------------------------------------------------ Driver

    def process(self, deployment: Deployment) -> Deployment:
        """Run the deployment to its end and clean up. Raises ``Interrupted`` if the daemon is asked to stop first."""
        if deployment.started_at is None:
            deployment.started_at = self.now()
        self._prepare(deployment)
        target = self.target
        steps = {
            Phase.QUEUED: target.begin,
            Phase.PULLING: target.pull,
            Phase.DRAINING: target.drain,
            Phase.SWAPPING: target.swap,
            Phase.VERIFYING: target.verify,
            Phase.ROLLING_BACK: target.roll_back,
        }
        while not deployment.finished:
            self.check_stop()
            # Each step either moves the record to another phase or finishes it, so the loop always makes progress.
            steps[deployment.phase](deployment)
        target.clean_up(deployment)
        return deployment

    def _prepare(self, deployment: Deployment) -> None:
        """Settle what the phases need: which compose file the deployment uses, and the registry login.

        The login and the choice of the compose file belong to the start of a deployment, before anything is pulled. A
        deployment that goes on after a restart only takes up the compose file it began with, so that it ends with the
        settings it started with. If the start fails, the deployment ends as failed before it touches the host.
        """
        docker = self.docker
        if self.compose is not None and docker is not None:
            docker.use_compose(self._recorded_compose(deployment))
        if deployment.phase not in (Phase.QUEUED, Phase.PULLING):
            return
        if self.registry is not None:
            problem = self.registry.login()
            if problem:
                self.finish(
                    deployment, Outcome.FAILED, FailureKind.PULL_FAILED, f"{problem}. Nothing on the host was touched."
                )
                return
        if self.compose is not None and docker is not None and not deployment.compose_settled:
            self._settle_compose(deployment, docker, self.compose)

    @staticmethod
    def _recorded_compose(deployment: Deployment) -> Path | None:
        """The compose file that the record names, or None for the compose file of the host."""
        if not deployment.compose_file:
            return None
        path = Path(deployment.compose_file)
        if not path.is_file():
            raise DockerError(f"the compose file of this deployment is missing: {path}")
        return path

    def _settle_compose(self, deployment: Deployment, docker: Docker, compose: ComposeSource) -> None:
        """Take the compose file out of the image of the tag, if the host is set to, and check it before it is used."""
        if not compose.enabled():
            deployment.compose_settled = True
            self.store.save(deployment)
            return
        try:
            fetched = compose.fetch(deployment)
        except ComposeError as exc:
            self.finish(deployment, Outcome.FAILED, FailureKind.PULL_FAILED, f"{exc}. Nothing on the host was touched.")
            return
        if fetched is None:
            deployment.compose_settled = True
            self.event(deployment, "the image of the tag carries no compose file: using the compose file of the host")
            self.store.save(deployment)
            return
        docker.use_compose(fetched.path)
        checked = docker.validate(deployment.tag)
        if not checked.ok:
            docker.use_compose(None)
            self.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.PULL_FAILED,
                f"the compose file of tag {deployment.tag} does not work with the .env file of this host: "
                f"{checked.tail()}. Nothing on the host was touched.",
            )
            return
        deployment.compose_file = str(fetched.path)
        deployment.compose_sha256 = fetched.sha256
        if self.target.can_roll_back:
            current = compose.current_path()
            deployment.previous_compose_file = str(current) if current else ""
        deployment.compose_settled = True
        self.event(
            deployment,
            f"using the compose file {fetched.path.name} of tag {deployment.tag} (sha256 {fetched.sha256[:12]})",
        )
        self.store.save(deployment)

    def _remember_compose(self, deployment: Deployment, compose: ComposeSource) -> None:
        """Note which compose file ran last, for the rollback of the next deployment. A problem here is only logged."""
        try:
            compose.remember(deployment)
        except OSError:
            logger.warning("Deployment %s: the compose file that ran was not noted", deployment.id, exc_info=True)

    def give_up(self, deployment: Deployment, error: Exception) -> None:
        """End the attempts after the manager itself failed again and again in the phase of the record.

        Before the swap nothing was replaced, so the deployment just fails. From the swap on, the host may run a
        version that nobody checked, so the previous version is restored first where there is one. The rollback gets
        attempts of its own. If it fails as well, the deployment ends as a failed rollback and every image stays.
        """
        reason = f"the node manager failed {deployment.attempts} times: {error}"
        logger.warning("Deployment %s: giving up in phase %s: %s", deployment.id, deployment.phase.value, reason)
        if deployment.phase is Phase.ROLLING_BACK:
            self.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.ROLLBACK_FAILED,
                f"{deployment.message}. The rollback failed too: {reason}.",
            )
        elif deployment.phase in (Phase.SWAPPING, Phase.VERIFYING):
            deployment.failure_logs = self.target.capture_logs()
            self.fail_over(deployment, FailureKind.INTERNAL_ERROR, reason)
        else:
            self.finish(deployment, Outcome.FAILED, FailureKind.INTERNAL_ERROR, reason)

    # ------------------------------------------------------------------ Record helpers

    def event(self, deployment: Deployment, note: str) -> None:
        if note:
            logger.info("Deployment %s (%s): %s", deployment.id, deployment.phase.value, note)
        deployment.events.append({"at": self.now(), "phase": deployment.phase.value, "note": note})
        del deployment.events[:-MAX_EVENTS]

    def advance(self, deployment: Deployment, phase: Phase, note: str = "") -> None:
        deployment.phase = phase
        # A new phase is progress, so errors of the phase before do not count against this one. This also gives a
        # rollback that starts after repeated errors attempts of its own.
        deployment.attempts = 0
        self.event(deployment, note)
        self.store.save(deployment)

    def finish(
        self, deployment: Deployment, outcome: Outcome, failure: FailureKind | None = None, message: str = ""
    ) -> None:
        # Before the record says it is over: a stop in between then repeats this step instead of losing the release.
        self.target.before_finish(deployment)
        compose = self.compose
        if compose is not None and self.target.can_roll_back and outcome in (Outcome.SUCCEEDED, Outcome.UNCHANGED):
            self._remember_compose(deployment, compose)
        deployment.outcome = outcome
        deployment.failure = failure
        deployment.message = message
        deployment.finished_at = self.now()
        deployment.phase = Phase.DONE
        self.event(deployment, f"{outcome.value}: {message}" if message else outcome.value)
        self.store.save(deployment)
        if self.orchestrator is not None and not deployment.from_logos:
            # Every ending passes here, also the one by ``give_up``: this is where a report becomes due. A deployment
            # that came from Logos is reported to Logos instead (see ``logos_link``).
            self._pending_reports.setdefault(deployment.id, _PendingReport(due_at=self.clock()))

    def fail_over(self, deployment: Deployment, kind: FailureKind, reason: str) -> None:
        """The new version is bad: the target restores the previous one, or the deployment ends as failed."""
        self.target.fail_over(deployment, kind, reason)

    def progress(self, deployment: Deployment, text: str) -> None:
        """Record a line about a wait, unless it repeats the last one or came too soon after it."""
        now = self.clock()
        last = self._last_progress
        if last is not None and (last[0] == text or now - last[1] < PROGRESS_EVENT_EVERY_S):
            return
        self._last_progress = (text, now)
        self.event(deployment, text)
        self.store.save(deployment)

    def reset_progress(self) -> None:
        """Forget the last line of a wait, so the next wait starts with a fresh line."""
        self._last_progress = None

    # ------------------------------------------------------------------ Waiting

    def ask(self, call: Callable[[], _T], *, stoppable: bool = True) -> _T:
        """Run a call to the orchestrator, asking again while it cannot be reached.

        Raises ``OrchestratorUnavailable`` when the retry window (``orchestrator_retry_window_s``) is over, or at once
        for an answer that asking again will not change.
        """
        window_s = self.settings.orchestrator_retry_window_s
        started = self.clock()
        while True:
            try:
                return call()
            except OrchestratorUnavailable as exc:
                if not exc.retryable or self.clock() - started >= window_s:
                    raise
                logger.warning("The orchestrator did not answer (%s); asking again", exc)
                if stoppable:
                    self.check_stop()
                self.pause()

    def pause(self) -> None:
        self.sleep(self.settings.poll_interval_s)

    def check_stop(self) -> None:
        if self.should_stop():
            raise Interrupted()

    def seconds_since(self, stamp: str | None) -> float:
        """Seconds from a time saved in a record to now; 0 if there is none or it cannot be read."""
        if not stamp:
            return 0.0
        return self.seconds_between(stamp, self.now())

    @staticmethod
    def seconds_between(start: str, end: str) -> float:
        """Seconds from one time saved in a record to a later one; 0 if one cannot be read or the end is earlier."""
        try:
            return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------ Report

    def report_pending(self) -> None:
        """Tell the orchestrator how the finished deployments ended, so that it notifies the admins.

        Every ending that is not yet reported and is recent enough is reported once, a deployment that changed nothing
        included. The endings come from one look at the records, at the first call, and from every ending after that.
        Each call makes one try for every report that is due and then returns, so a report that fails does not hold up
        the next deployment. A report that failed is tried again after a pause that doubles up to a limit, for as long
        as the ending is recent. The record keeps when the orchestrator accepted the report, so a stop between the end
        of the deployment and the report leaves the report for the next start. Without an orchestrator to ask, this
        does nothing.
        """
        client = self.orchestrator
        if client is None:
            return
        if not self._looked_for_unreported:
            self._look_for_unreported()
        for deployment_id, pending in list(self._pending_reports.items()):
            if pending.due_at > self.clock():
                continue
            self.check_stop()
            deployment = self.store.load(deployment_id)
            if (
                deployment is None
                or not deployment.finished
                or deployment.outcome is None
                or deployment.reported_at is not None
                or self.seconds_since(deployment.finished_at) > REPORT_MAX_AGE_S
            ):
                del self._pending_reports[deployment_id]
                continue
            try:
                client.report(deployment.id, self._report_details(deployment))
            except OrchestratorUnavailable as exc:
                if exc.retryable:
                    pending.failures += 1
                    pause = min(REPORT_RETRY_FIRST_S * 2 ** (pending.failures - 1), REPORT_RETRY_MAX_S)
                    pending.due_at = self.clock() + pause
                    logger.warning(
                        "Deployment %s: the ending was not reported: %s. Trying again in %d s",
                        deployment.id,
                        exc,
                        pause,
                    )
                else:
                    # An answer that asking again will not change. The next start looks at the record once more.
                    del self._pending_reports[deployment_id]
                    logger.warning("Deployment %s: the ending was not reported: %s", deployment.id, exc)
                continue
            deployment.reported_at = self.now()
            self.store.save(deployment)
            del self._pending_reports[deployment_id]
            logger.info("Deployment %s: the ending was reported to the orchestrator", deployment.id)

    def _look_for_unreported(self) -> None:
        """Find the endings that were not reported before this start. The one time the records are read for reports."""
        for deployment in self.store.all():
            if (
                deployment.finished
                and deployment.outcome is not None
                and deployment.reported_at is None
                and not deployment.from_logos
            ):
                self._pending_reports.setdefault(deployment.id, _PendingReport(due_at=self.clock()))
        self._looked_for_unreported = True

    def _report_details(self, deployment: Deployment) -> dict:
        drain_seconds = None
        if deployment.cordoned_at and deployment.drained_at:
            drain_seconds = int(self.seconds_between(deployment.cordoned_at, deployment.drained_at))
        return {
            "tag": deployment.tag,
            "outcome": deployment.outcome.value if deployment.outcome else "",
            "failure": deployment.failure.value if deployment.failure else None,
            "message": deployment.message[:_MAX_REPORT_MESSAGE_CHARS],
            "requested_by": deployment.requested_by[:_MAX_REPORT_ORIGIN_CHARS],
            "drained_requests": deployment.drained_requests,
            "drain_seconds": drain_seconds,
            "sole_replica_models": deployment.sole_replica_models,
        }
