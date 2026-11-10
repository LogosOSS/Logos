"""The core target: replace the services of the Logos compose project on a core host.

A core deployment pulls the images of every service for the tag, lets Compose replace the services whose image or
configuration changed, and checks that all services run and that Logos answers. There is no drain: the core has no node
to cordon. There is no rollback either. The new webservice changes the database structure when it starts, so the
previous version may not work with the data any more, and a rollback would hide that. A failed core deployment ends as
``failed`` and a person decides: fix forward, or restore a backup.

The manager that runs this sits outside the compose project it replaces. Otherwise the ``up`` would restart the manager
in the middle of its own deployment.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Callable

from logos_node_manager.docker_ops import ProjectContainer, ProjectDocker, read_env_value
from logos_node_manager.state import Deployment, FailureKind, Outcome, Phase

if TYPE_CHECKING:
    from logos_node_manager.deployer import Deployer

logger = logging.getLogger(__name__)

# The saved log of failed services is limited, so the record stays small.
MAX_LOG_CHARS = 6000
# The registry Compose uses when the ``.env`` file names none.
DEFAULT_REGISTRY = "ghcr.io/logososs"
NO_ROLLBACK = (
    "The core has no automatic rollback: some services may run the new version and some the old one. "
    "Fix the cause and deploy again, or restore a backup."
)
_HEALTH_PROBE_TIMEOUT_S = 10.0


def probe_url(url: str) -> bool:
    """Whether the address answers with a success status."""
    try:
        with urllib.request.urlopen(url, timeout=_HEALTH_PROBE_TIMEOUT_S) as response:  # noqa: S310 - http(s) only
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


class CoreTarget:
    can_roll_back = False

    def __init__(
        self, deployer: "Deployer", docker: ProjectDocker, *, probe: Callable[[str], bool] = probe_url
    ) -> None:
        self.d = deployer
        self.docker = docker
        self._probe = probe
        # The services that the last check found wrong, for the log that goes into the record.
        self._failing: list[str] = []

    # ------------------------------------------------------------------ Steps

    def begin(self, deployment: Deployment) -> None:
        """Note which containers run now, so that the check can tell the new ones from the old ones."""
        containers = self.docker.project_containers()
        deployment.previous_container_ids = [container.info.id for container in containers]
        running = sum(1 for container in containers if container.info.running)
        self.d.advance(
            deployment,
            Phase.PULLING,
            f"{running} of {len(containers)} container(s) run now; deploying tag {deployment.tag}",
        )

    def pull(self, deployment: Deployment) -> None:
        result = self.docker.pull_project(deployment.tag)
        if not result.ok:
            self.d.finish(
                deployment,
                Outcome.FAILED,
                FailureKind.PULL_FAILED,
                f"docker compose pull failed: {result.tail()}. The running services were not touched.",
            )
            return
        self.d.advance(deployment, Phase.DRAINING, f"pulled the images for tag {deployment.tag}")

    def drain(self, deployment: Deployment) -> None:
        """The core has no node to cordon: the phase passes at once."""
        self.d.advance(deployment, Phase.SWAPPING)

    def swap(self, deployment: Deployment) -> None:
        replicas = self.docker.replicas()
        result = self.docker.up_project(deployment.tag, replicas)
        if not result.ok:
            self._failing = []
            self.fail_over(deployment, FailureKind.SWAP_FAILED, f"docker compose up failed: {result.tail()}")
            return
        if self._replaced_nothing(deployment):
            # Compose found nothing to replace. That does not show that the services are fine: a deployment that failed
            # its checks leaves the same containers behind. So the checks run all the same, and the deployment ends as
            # ``unchanged`` if they pass. The ids are those of the start of this deployment, so a step that is repeated
            # after the replacement cannot take the new containers for old ones.
            self.d.advance(
                deployment, Phase.VERIFYING, "no service needed replacing; checking that all of them are ready"
            )
            return
        self.d.advance(deployment, Phase.VERIFYING, f"started the services for tag {deployment.tag}")

    def verify(self, deployment: Deployment) -> None:
        """Wait until every service runs, Logos answers, and all of it holds for the stability window."""
        d = self.d
        settings = d.settings
        deadline = d.clock() + settings.core_verify_timeout_s
        ready_since: float | None = None
        while True:
            d.check_stop()
            problem, waiting = self._check(deployment)
            if problem:
                self._fail(deployment, problem)
                return
            if waiting is None:
                now = d.clock()
                ready_since = now if ready_since is None else ready_since
                if now - ready_since >= settings.stability_window_s:
                    if self._replaced_nothing(deployment):
                        d.finish(deployment, Outcome.UNCHANGED, message="every service already runs this version")
                    else:
                        d.finish(deployment, Outcome.SUCCEEDED, message=f"now running tag {deployment.tag}")
                    return
            else:
                ready_since = None
                if d.clock() >= deadline:
                    self._fail(deployment, f"not ready after {int(settings.core_verify_timeout_s)} s: {waiting}")
                    return
                d.progress(deployment, f"waiting: {waiting}")
            d.pause()

    def _replaced_nothing(self, deployment: Deployment) -> bool:
        before = set(deployment.previous_container_ids)
        return bool(before) and {container.info.id for container in self.docker.project_containers()} == before

    def _fail(self, deployment: Deployment, reason: str) -> None:
        deployment.failure_logs = self.capture_logs()
        self.fail_over(deployment, FailureKind.VERIFY_FAILED, reason)

    def _check(self, deployment: Deployment) -> tuple[str | None, str | None]:
        """What is wrong with the project, and what it still waits for. ``(None, None)`` means it is ready.

        Containers that are new since the start of the deployment must not have restarted: the restart policy brings a
        crashing service back, so a count above zero means it crashed while it started. A container that was there
        before keeps whatever count it had.
        """
        settings = self.d.settings
        before = set(deployment.previous_container_ids)
        containers = self.docker.project_containers()
        by_service: dict[str, list[ProjectContainer]] = {}
        for container in containers:
            by_service.setdefault(container.service, []).append(container)

        failing: list[str] = []
        problem: str | None = None
        for container in containers:
            info = container.info
            fresh = info.id not in before
            name = container.service or info.id[:12]
            if fresh and (info.status in ("exited", "dead") and info.exit_code != 0):
                problem = problem or f"{name} exited with code {info.exit_code}"
            elif fresh and (info.restarting or info.restart_count > 0):
                problem = problem or f"{name} restarted {info.restart_count} time(s)"
            elif info.health == "unhealthy":
                problem = problem or f"the health check reports {name} as unhealthy"
            else:
                continue
            failing.append(container.service)
        if problem:
            self._failing = [service for service in dict.fromkeys(failing) if service]
            return problem, None

        waiting: list[str] = []
        replicas = self.docker.replicas()
        for service in self.docker.services():
            wanted = replicas if service == settings.scale_service else 1
            ready = sum(
                1
                for container in by_service.get(service, [])
                if container.info.running and container.info.health in (None, "healthy")
            )
            if ready < wanted:
                waiting.append(f"{service} runs {ready} of {wanted} ready cop{'y' if wanted == 1 else 'ies'}")
        if not waiting and settings.core_health_url and not self._probe(settings.core_health_url):
            waiting.append(f"{settings.core_health_url} does not answer yet")
        self._failing = [text.split(" ", 1)[0] for text in waiting if not text.startswith("http")]
        return None, "; ".join(waiting) if waiting else None

    def fail_over(self, deployment: Deployment, kind: FailureKind, reason: str) -> None:
        """There is no previous version to restore: the deployment ends as failed."""
        deployment.failure = kind
        deployment.message = reason
        self.d.finish(deployment, Outcome.FAILED, kind, f"{reason}. {NO_ROLLBACK}")

    def roll_back(self, deployment: Deployment) -> None:
        """A core record never reaches the rollback phase. A record from elsewhere that does ends as failed."""
        self.d.finish(
            deployment,
            Outcome.FAILED,
            FailureKind.ROLLBACK_FAILED,
            f"{deployment.message.rstrip('.')}. {NO_ROLLBACK}" if deployment.message else NO_ROLLBACK,
        )

    def before_finish(self, deployment: Deployment) -> None:
        """The core holds nothing that has to be given back."""

    def capture_logs(self) -> str:
        """The end of the log of the services that failed. Never raises."""
        try:
            return self.docker.service_logs(self._failing, 30)[-MAX_LOG_CHARS:]
        except Exception:  # noqa: BLE001 - the log is a bonus; it must never hide the real failure
            logger.warning("The log of the failed services could not be read", exc_info=True)
            return ""

    # ------------------------------------------------------------------ Clean-up

    def clean_up(self, deployment: Deployment) -> None:
        """After a success: remove the images no container uses, then pull the image of the agent sessions.

        The agent image is not a service of the project: the agent runner starts containers from it through the Docker
        socket. Nothing references it between sessions, so the prune would delete it. It is pulled after the prune and
        not before. After a failure every image stays, because a person may need them to see what went wrong.

        A problem here is logged and does not change the result.
        """
        if deployment.outcome is not Outcome.SUCCEEDED:
            return
        try:
            pruned = self.docker.prune_images()
            if not pruned.ok:
                logger.warning("Deployment %s: the unused images were not removed: %s", deployment.id, pruned.tail(2))
            env_file = self.d.settings.env_file
            profiles = re.split(r"[,\s]+", read_env_value(env_file, "COMPOSE_PROFILES") or "")
            if "agent" in profiles:
                registry = read_env_value(env_file, "REGISTRY") or DEFAULT_REGISTRY
                reference = f"{registry}/logos-agent-workspace:{deployment.tag}"
                pulled = self.docker.pull_image(reference)
                if not pulled.ok:
                    logger.warning("Deployment %s: %s was not pulled: %s", deployment.id, reference, pulled.tail(2))
        except Exception:  # noqa: BLE001 - the deployment is over; a clean-up problem must not change its result
            logger.warning("Deployment %s: the clean-up failed", deployment.id, exc_info=True)
