"""The manager replaces itself with a newer version of its own image.

A container cannot do this alone. Compose stops the old container before it starts the new one, and with it the process
that does the work, so the replacement would end half done. A second container, the helper, does the swap.

The manager (this module) decides, prepares and hands over:

1. It works out which image its own service names for the tag, pulls that image and compares it with the one it runs.
   The same image means there is nothing to do.
2. If the images differ, it saves a record and starts the helper from its own current image, the version that is known
   to work. The helper has the same mounts and the same hardening as the manager. It is not a part of the compose
   project, so replacing the manager does not touch it.
3. From then on the manager starts no deployment. It only watches the helper.

The helper (see ``self_update_helper``) swaps, checks and, if the new manager does not come up, restores the old one,
the way the rollback of a worker does. Every step goes into the record ``<state dir>/self-update/<id>.json``. If the
helper ends without a result, the manager that runs at that moment, the old or the new one, finishes the record from
what Docker shows. The new manager removes the old image and the stopped helper after the record is finished.

A request comes from the command line (``node-manager self-update``) or, if the host switched it on, from the end of a
deployment that went well. It is queued as a record, like a deployment, and the daemon runs it when no deployment waits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from logos_node_manager.compose_source import ComposeSource
from logos_node_manager.config import Settings
from logos_node_manager.docker_ops import QUERY_TIMEOUT_S, ContainerInfo, Docker, read_env_value, split_image_ref
from logos_node_manager.errors import ComposeError, DockerError, Interrupted, StateError
from logos_node_manager.registry import RegistryLogin
from logos_node_manager.runner import Runner
from logos_node_manager.state import (
    MAX_EVENTS,
    PREVIOUS_TAG,
    Deployment,
    Outcome,
    create_exclusively,
    utc_now,
    validate_id,
    validate_tag,
    write_atomically,
)
from logos_node_manager.targets import short_id

logger = logging.getLogger(__name__)

# The switch in the .env file of the host. It is off by default, and a change needs no restart: it is read each time.
SWITCH_NAME = "NODE_MANAGER_SELF_UPDATE"
# The helper has a fixed name, so that Docker allows one helper at a time.
HELPER_NAME = "nm-self-update"
# How long a helper may have the swap before the manager takes it for lost. A swap, the checks and a rollback with all
# its attempts take about half of this.
HELPER_MAX_S = 3600.0
# After this many unexpected errors of the manager itself, a request is given up.
MAX_ATTEMPTS = 3
KEEP_FINISHED = 10
# Time for ``docker run -d``, which returns as soon as the container has started.
HELPER_START_TIMEOUT_S = 60.0
_TRUE = {"1", "true", "yes", "on"}
# The helper gets the settings of the manager. A name that holds a secret stays out: the helper has no use for it.
_SECRET_NAME = re.compile(r"(KEY|PASSWORD|TOKEN|SECRET)$")


class UpdatePhase(str, Enum):
    QUEUED = "queued"
    # The helper has the swap. The manager that handed it over starts no deployment until the helper is done.
    HANDED_OVER = "handed_over"
    SWAPPING = "swapping"
    VERIFYING = "verifying"
    ROLLING_BACK = "rolling_back"
    DONE = "done"


# The phases in which the helper owns the record.
HELPER_PHASES = (UpdatePhase.HANDED_OVER, UpdatePhase.SWAPPING, UpdatePhase.VERIFYING, UpdatePhase.ROLLING_BACK)


class Failure(str, Enum):
    # This manager cannot replace itself where it runs, for example because it was not made by Compose.
    NOT_ELIGIBLE = "not_eligible"
    PULL_FAILED = "pull_failed"
    # The helper could not be started, or ended without a result.
    HELPER_FAILED = "helper_failed"
    SWAP_FAILED = "swap_failed"
    VERIFY_FAILED = "verify_failed"
    ROLLBACK_FAILED = "rollback_failed"
    # The helper did not finish in the time it gets.
    TIMED_OUT = "timed_out"
    INTERNAL_ERROR = "internal_error"


@dataclass
class SelfUpdate:
    """One request to replace the manager with the image of a tag, and everything learned while it ran."""

    id: str
    tag: str
    requested_at: str
    requested_by: str = ""
    phase: UpdatePhase = UpdatePhase.QUEUED
    outcome: Outcome | None = None
    failure: Failure | None = None
    message: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    # Unexpected errors of the manager itself in the phase of the record.
    attempts: int = 0
    # When the helper got the swap. The watch over the helper counts its limit from here.
    handed_over_at: str | None = None

    # The container and the image that run now. The names are those of the fields of a deployment, because the rollback
    # code reads them.
    previous_container_id: str | None = None
    previous_image_id: str | None = None
    previous_image_ref: str | None = None
    # The image to run instead, and the container that was made from it.
    target_image_ref: str | None = None
    target_image_id: str | None = None
    new_container_id: str | None = None

    # The compose file for the swap, and the one for the way back. Empty means the compose file of the host for the
    # manager's own service. The first is a file that was taken out of the image of the tag. The second is the file that
    # the running manager was made from, if it still exists.
    compose_file: str = ""
    compose_sha256: str = ""
    rollback_compose_file: str = ""

    # Whether the images that the request left without a purpose were removed. Set once the record is finished.
    cleaned_up: bool = False
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.phase is UpdatePhase.DONE

    def to_dict(self) -> dict:
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["phase"] = self.phase.value
        data["outcome"] = self.outcome.value if self.outcome else None
        data["failure"] = self.failure.value if self.failure else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SelfUpdate":
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        try:
            known["phase"] = UpdatePhase(known.get("phase", UpdatePhase.QUEUED.value))
            known["outcome"] = Outcome(known["outcome"]) if known.get("outcome") else None
            known["failure"] = Failure(known["failure"]) if known.get("failure") else None
            return cls(**known)
        except (ValueError, TypeError) as exc:
            raise StateError(f"invalid self-update record: {exc}") from exc


class SelfUpdateStore:
    """The records of the self-updates: one JSON file each, replaced atomically on every write."""

    def __init__(self, state_dir: Path) -> None:
        self.directory = state_dir / "self-update"
        # The compose files that were taken out of images. One directory for each request, named ``self-<id>``.
        self.compose_dir = state_dir / "compose"

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, update_id: str) -> Path:
        return self.directory / f"{validate_id(update_id)}.json"

    @staticmethod
    def _dump(record: SelfUpdate) -> str:
        return json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"

    def submit(self, record: SelfUpdate) -> SelfUpdate:
        """Queue a request. A second request with the same id returns the first one, so a retry attaches to it."""
        self.ensure()
        validate_tag(record.tag)
        if create_exclusively(self._path(record.id), self._dump(record)):
            return record
        existing = self.load(record.id)
        if existing is None:
            # The file exists but is not readable yet: another submit is writing it.
            time.sleep(0.2)
            existing = self.load(record.id)
        if existing is None:
            raise StateError(f"self-update {record.id} exists but cannot be read")
        if existing.tag != record.tag:
            raise StateError(f"self-update id {record.id} is already used for tag {existing.tag}")
        return existing

    def load(self, update_id: str) -> SelfUpdate | None:
        """The record, or None if there is none or it is not readable yet."""
        path = self._path(update_id)
        try:
            return SelfUpdate.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, StateError) as exc:
            logger.warning("Self-update record %s is not readable: %s", path.name, exc)
            return None

    def save(self, record: SelfUpdate) -> None:
        self.ensure()
        write_atomically(self._path(record.id), self._dump(record))

    def all(self) -> list[SelfUpdate]:
        """Every readable record, oldest request first."""
        if not self.directory.is_dir():
            return []
        records = []
        for path in self.directory.glob("*.json"):
            try:
                validate_id(path.stem)
            except ValueError:
                continue
            record = self.load(path.stem)
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda record: (record.requested_at, record.id))

    def active(self) -> SelfUpdate | None:
        """The oldest request that is not finished."""
        return next((record for record in self.all() if not record.finished), None)

    def latest(self) -> SelfUpdate | None:
        records = self.all()
        return records[-1] if records else None

    def prune(self, keep: int = KEEP_FINISHED) -> None:
        """Delete the oldest finished records beyond ``keep``, and the compose files that belong to them."""
        finished = [record for record in self.all() if record.finished]
        for record in finished[: max(0, len(finished) - keep)]:
            self._path(record.id).unlink(missing_ok=True)
            shutil.rmtree(self.compose_dir / f"self-{record.id}", ignore_errors=True)


class Recorder:
    """What the manager and the helper share: a record that is written to the disk, and its events."""

    def __init__(self, store: SelfUpdateStore, now: Callable[[], str]) -> None:
        self.store = store
        self.now = now

    def event(self, record: SelfUpdate, note: str) -> None:
        if note:
            logger.info("Self-update %s (%s): %s", record.id, record.phase.value, note)
        record.events.append({"at": self.now(), "phase": record.phase.value, "note": note})
        del record.events[:-MAX_EVENTS]

    def advance(self, record: SelfUpdate, phase: UpdatePhase, note: str = "") -> None:
        record.phase = phase
        record.attempts = 0
        self.event(record, note)
        self.store.save(record)

    def finish(self, record: SelfUpdate, outcome: Outcome, failure: Failure | None = None, message: str = "") -> None:
        record.outcome = outcome
        record.failure = failure
        record.message = message
        record.finished_at = self.now()
        record.phase = UpdatePhase.DONE
        self.event(record, f"{outcome.value}: {message}" if message else outcome.value)
        self.store.save(record)


@dataclass(frozen=True)
class HelperState:
    """What Docker says about the helper container."""

    running: bool
    status: str
    exit_code: int


class Helper:
    """The helper container: start it, look at it, read its log and remove it."""

    def __init__(self, settings: Settings, runner: Runner, environ: Mapping[str, str]) -> None:
        self._settings = settings
        self._runner = runner
        self._environ = environ

    def command(self, image_id: str, update_id: str, source: str) -> list[str]:
        """The command that starts the helper.

        It runs the image that the manager runs. It has the volumes of the container ``source``, which is the manager
        (the Docker socket, the project directory and the state directory, with the same modes), the same hardening,
        and no network: it only talks to the Docker socket. The settings of the manager reach it as variables of the
        same names. A variable is named here without its value, so that Docker takes the value from the environment of
        this command, and no value is on any command line.
        """
        argv = [
            "docker",
            "run",
            "-d",
            "--name",
            HELPER_NAME,
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "DAC_OVERRIDE",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=16m",
            "--volumes-from",
            source,
        ]
        for name in sorted(self._environ):
            if name.startswith("NODE_MANAGER_") and not _SECRET_NAME.search(name):
                argv += ["-e", name]
        return [*argv, image_id, "self-update-run", update_id]

    def start(self, image_id: str, update_id: str, source: str) -> str | None:
        """Start the helper. Returns None if it runs, or the reason that it does not."""
        existing = self.state()
        if existing is not None:
            if existing.running:
                return "another helper is running"
            # What an earlier helper left behind would make the name a conflict.
            self.remove()
        result = self._runner.run(self.command(image_id, update_id, source), timeout_s=HELPER_START_TIMEOUT_S)
        return None if result.ok else result.tail(4)

    def state(self) -> HelperState | None:
        """The state of the helper, or None if there is no such container."""
        result = self._runner.run(["docker", "inspect", "--type", "container", HELPER_NAME], timeout_s=QUERY_TIMEOUT_S)
        if not result.ok:
            if "no such" in result.stderr.lower():
                return None
            raise DockerError(f"docker inspect failed: {result.tail()}")
        try:
            state = json.loads(result.stdout)[0].get("State") or {}
            return HelperState(
                running=bool(state.get("Running")),
                status=str(state.get("Status") or ""),
                exit_code=int(state.get("ExitCode") or 0),
            )
        except (ValueError, IndexError, TypeError, AttributeError) as exc:
            raise DockerError(f"docker inspect returned unexpected output: {exc}") from exc

    def logs(self, lines: int = 20) -> str:
        result = self._runner.run(["docker", "logs", "--tail", str(lines), HELPER_NAME], timeout_s=QUERY_TIMEOUT_S)
        return "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)

    def remove(self) -> None:
        """Remove the helper, a running one included. A helper that is not there is no problem."""
        self._runner.run(["docker", "rm", "-f", HELPER_NAME], timeout_s=QUERY_TIMEOUT_S)


class SelfUpdater(Recorder):
    """The part of the self-update that the daemon does: request, prepare, hand over, watch and clean up.

    ``docker`` is aimed at the manager's own service (see ``self_docker``), ``runner`` runs the commands for the helper.
    ``registry`` logs in before the pull, and ``compose`` takes the compose file of the manager's service out of the
    image of the tag. Without them the update uses the login and the compose file of the host.
    """

    def __init__(
        self,
        settings: Settings,
        docker: Docker,
        store: SelfUpdateStore,
        runner: Runner,
        *,
        registry: RegistryLogin | None = None,
        compose: ComposeSource | None = None,
        should_stop: Callable[[], bool] = lambda: False,
        now: Callable[[], str] = utc_now,
        hostname: Callable[[], str] = socket.gethostname,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(store, now)
        self.settings = settings
        self.docker = docker
        self.registry = registry
        self.compose = compose
        self.should_stop = should_stop
        self._hostname = hostname
        self.helper = Helper(settings, runner, os.environ if environ is None else environ)

    # ------------------------------------------------------------------ What the .env file says

    def enabled(self) -> bool:
        """Whether a deployment that went well starts a self-update. Read from the ``.env`` file each time."""
        raw = read_env_value(self.settings.env_file, SWITCH_NAME)
        return bool(raw) and raw.strip().lower() in _TRUE

    def describe(self) -> str:
        """One line for the log at start."""
        s = self.settings
        own = f"the service {s.self_service} of the project {s.self_project_name}"
        if self.enabled():
            return f"after each deployment that went well, the manager updates itself ({own})"
        return f"the manager updates itself only on request ({SWITCH_NAME} is off in the .env file; {own})"

    # ------------------------------------------------------------------ Asking for an update

    def after_deployment(self, deployment: Deployment) -> None:
        """Queue an update of the manager to the tag of a deployment that ended well, if the host is set to.

        A deployment that failed or was rolled back says nothing good about its release, so the manager stays as it is.
        The request has the id of the deployment, so a deployment is followed by at most one request.
        """
        if not self.enabled() or not deployment.finished:
            return
        if deployment.outcome not in (Outcome.SUCCEEDED, Outcome.UNCHANGED):
            return
        try:
            self.store.submit(
                SelfUpdate(
                    id=deployment.id,
                    tag=deployment.tag,
                    requested_at=self.now(),
                    requested_by=f"deployment {deployment.id}",
                )
            )
        except (ValueError, StateError):
            logger.warning("No self-update was queued for deployment %s", deployment.id, exc_info=True)

    # ------------------------------------------------------------------ What the daemon asks every pass

    def step(self, *, deployments_pending: bool) -> bool:
        """Do what a self-update needs from the daemon. True while a helper has the swap: no deployment may start.

        A request that is queued runs when no deployment waits. A problem of the manager itself is counted, and after a
        few the request is given up, so that one bad record cannot hold the daemon.
        """
        record = self.store.active()
        if record is None:
            self._clean_up()
            return False
        try:
            if record.phase in HELPER_PHASES:
                return self._watch_helper(record)
            if deployments_pending:
                return False
            self._run_queued(record)
            return record.phase in HELPER_PHASES
        except Interrupted:
            return False
        except Exception as exc:  # noqa: BLE001 - one bad request must not stop the daemon
            logger.exception("Self-update %s hit an unexpected error", record.id)
            record.attempts += 1
            if record.attempts >= MAX_ATTEMPTS:
                self.finish(
                    record,
                    Outcome.FAILED,
                    Failure.INTERNAL_ERROR,
                    f"the manager failed {record.attempts} times: {exc}",
                )
            else:
                self.store.save(record)
            return False

    def check_stop(self) -> None:
        if self.should_stop():
            raise Interrupted()

    # ------------------------------------------------------------------ Prepare

    def _run_queued(self, record: SelfUpdate) -> None:
        """Check that the manager can replace itself, get the image, and hand the swap to the helper."""
        if record.started_at is None:
            record.started_at = self.now()
        newer = self._newer_request(record)
        if newer is not None:
            # Two deployments in a row each queue a request. Only the tag of the last one matters, and swapping to the
            # older tag first would only add a swap.
            self.finish(
                record,
                Outcome.UNCHANGED,
                message=f"the request {newer.id} for tag {newer.tag} is newer and replaces this one. "
                "The manager was not touched.",
            )
            return
        me = self._identify(record)
        if me is None:
            return
        if self.registry is not None:
            problem = self.registry.login()
            if problem:
                self.finish(record, Outcome.FAILED, Failure.PULL_FAILED, f"{problem}. Nothing on the host was touched.")
                return
        self.check_stop()
        if not self._settle_compose(record):
            return
        self.check_stop()
        if not self._pull_target(record):
            return
        if record.target_image_id == me.image_id:
            self.finish(record, Outcome.UNCHANGED, message=f"the manager already runs {record.target_image_ref}")
            return
        record.previous_container_id = me.id
        record.previous_image_id = me.image_id
        record.previous_image_ref = me.image_ref
        record.rollback_compose_file = self._compose_of(me)
        self.check_stop()
        self._hand_over(record, me)

    def _newer_request(self, record: SelfUpdate) -> SelfUpdate | None:
        """The newest request that still waits and is newer than this one, or None."""
        newest = None
        for other in self.store.all():
            if other.finished or other.phase is not UpdatePhase.QUEUED or other.id == record.id:
                continue
            if (other.requested_at, other.id) > (record.requested_at, record.id):
                newest = other  # the records are sorted, oldest first
        return newest

    def _identify(self, record: SelfUpdate) -> ContainerInfo | None:
        """The container this manager runs in, if it can replace it. Otherwise the record ends as failed."""
        try:
            me = self.docker.container()
        except DockerError as exc:
            self.finish(
                record, Outcome.FAILED, Failure.INTERNAL_ERROR, f"the container of the manager cannot be read: {exc}"
            )
            return None
        problem = self._problem_with(me)
        if problem:
            self.finish(record, Outcome.FAILED, Failure.NOT_ELIGIBLE, f"{problem}. Nothing on the host was touched.")
            return None
        return me

    def _problem_with(self, me: ContainerInfo | None) -> str | None:
        """Why this manager cannot replace the container, or None.

        The checks make sure that the swap replaces this very container. Compose recreates a container only if it made
        it as that service of that project. Otherwise it would start a second container with the same name.
        """
        s = self.settings
        name = s.self_container
        if me is None:
            return (
                f"there is no container {name}, so this manager does not run as the service {s.self_service} "
                f"of the project {s.self_project_name}"
            )
        here = self._hostname()
        if not here or not me.id.startswith(here):
            return f"this manager runs in the container {here or 'unknown'}, not in {name}, so it cannot replace {name}"
        if me.project != s.self_project_name or me.service != s.self_service:
            return (
                f"the container {name} was not made by Compose as the service {s.self_service} of the project "
                f"{s.self_project_name} (it says {me.project or 'nothing'} and {me.service or 'nothing'}), so Compose "
                "would not replace it"
            )
        if not me.image_ref or me.image_ref.startswith("sha256:"):
            return f"the container {name} was made from an image ID, so there is no image name to go back to"
        return None

    def _compose_of(self, me: ContainerInfo) -> str:
        """The compose file that the running manager was made from, for the way back. Empty if it is not there."""
        paths = [part.strip() for part in (me.config_files or "").split(",") if part.strip()]
        if len(paths) == 1 and Path(paths[0]).is_file():
            return paths[0]
        return ""

    def _settle_compose(self, record: SelfUpdate) -> bool:
        """Choose the compose file for the swap and check it. False if the record ended as failed."""
        docker = self.docker
        docker.use_compose(None)
        record.compose_file = ""
        record.compose_sha256 = ""
        compose = self.compose
        if compose is not None and compose.enabled():
            try:
                fetched = compose.fetch(record, own=True)
            except ComposeError as exc:
                self.finish(record, Outcome.FAILED, Failure.PULL_FAILED, f"{exc}. Nothing on the host was touched.")
                return False
            if fetched is not None:
                docker.use_compose(fetched.path)
                checked = docker.validate(record.tag)
                if not checked.ok:
                    docker.use_compose(None)
                    self.finish(
                        record,
                        Outcome.FAILED,
                        Failure.PULL_FAILED,
                        f"the compose file of tag {record.tag} does not work with the .env file of this host: "
                        f"{checked.tail()}. Nothing on the host was touched.",
                    )
                    return False
                record.compose_file = str(fetched.path)
                record.compose_sha256 = fetched.sha256
                self.event(
                    record,
                    f"using the compose file {fetched.path.name} of tag {record.tag} (sha256 {fetched.sha256[:12]})",
                )
                return True
            self.event(
                record,
                "the image of the tag carries no compose file for the manager: using the compose file of the host",
            )
        if not self.settings.self_compose_file.is_file():
            self.finish(
                record,
                Outcome.FAILED,
                Failure.NOT_ELIGIBLE,
                f"the compose file of the manager does not exist: {self.settings.self_compose_file}. "
                "Nothing on the host was touched.",
            )
            return False
        return True

    def _pull_target(self, record: SelfUpdate) -> bool:
        """Get the image that the service names for the tag, and note it. False if the record ended as failed."""
        docker = self.docker
        result = docker.pull(record.tag)
        if not result.ok:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.PULL_FAILED,
                f"docker compose pull failed: {result.tail()}. The manager was not touched.",
            )
            return False
        try:
            reference = docker.image_ref(record.tag)
            image_id = docker.image_id(reference)
        except DockerError as exc:
            self.finish(record, Outcome.FAILED, Failure.PULL_FAILED, f"{exc}. The manager was not touched.")
            return False
        if image_id is None:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.PULL_FAILED,
                f"image {reference} is not on this host after the pull. The manager was not touched.",
            )
            return False
        record.target_image_ref = reference
        record.target_image_id = image_id
        return True

    # ------------------------------------------------------------------ Hand over

    def _hand_over(self, record: SelfUpdate, me: ContainerInfo) -> None:
        """Save the record, then start the helper. The helper runs the image of the manager that starts it."""
        record.handed_over_at = self.now()
        self.advance(
            record,
            UpdatePhase.HANDED_OVER,
            f"running {me.image_ref} ({short_id(me.image_id)}); handing the swap to {record.target_image_ref} "
            f"({short_id(record.target_image_id)}) to a helper",
        )
        try:
            problem = self.helper.start(me.image_id, record.id, me.id)
        except DockerError as exc:
            problem = str(exc)
        if problem:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.HELPER_FAILED,
                f"the helper could not be started: {problem}. The manager was not touched.",
            )

    # ------------------------------------------------------------------ Watch

    def _watch_helper(self, record: SelfUpdate) -> bool:
        """Wait while the helper runs. Finish the record if the helper ended without a result. True while it runs.

        A Docker that cannot be asked for a moment is no sign that the helper is gone, for example while the engine
        restarts. The manager then keeps waiting, until the time limit of the helper is over.
        """
        late = self._seconds_since(record.handed_over_at) >= HELPER_MAX_S
        too_long = (
            f"the helper did not finish within {int(HELPER_MAX_S)} s and was removed. Look at which manager runs "
            "(docker ps) and at the events of this record"
        )
        try:
            state = self.helper.state()
        except DockerError as exc:
            if not late:
                logger.warning("The helper of self-update %s cannot be read: %s", record.id, exc)
                return True
            self.finish(record, Outcome.FAILED, Failure.TIMED_OUT, too_long)
            return False
        if state is not None and state.running:
            if not late:
                return True
            self.helper.remove()
            self.finish(record, Outcome.FAILED, Failure.TIMED_OUT, too_long)
            return False
        # The helper is gone or stopped. Read the record again: it may have written its result just before it ended.
        latest = self.store.load(record.id) or record
        if latest.finished:
            self.helper.remove()
            return False
        self._adopt(latest, state)
        self.helper.remove()
        return False

    def _adopt(self, record: SelfUpdate, state: HelperState | None) -> None:
        """The helper ended without a result. Say how it ended from the manager that Docker shows."""
        ended = (
            "the helper container is gone" if state is None else f"the helper ended with exit code {state.exit_code}"
        )
        log = self.helper.logs(10) if state is not None else ""
        if log:
            ended += f": {log[-500:]}"
        try:
            info = self.docker.container()
        except DockerError as exc:
            self.finish(
                record,
                Outcome.FAILED,
                Failure.INTERNAL_ERROR,
                f"{ended}. The manager could not be read afterwards: {exc}",
            )
            return
        if info is not None and info.image_id == record.target_image_id:
            if record.phase is UpdatePhase.ROLLING_BACK:
                self.finish(
                    record,
                    Outcome.FAILED,
                    Failure.ROLLBACK_FAILED,
                    f"{ended}. The rollback did not finish: the new manager {record.target_image_ref} runs.",
                )
            else:
                self.finish(
                    record,
                    Outcome.SUCCEEDED,
                    message=f"now running {record.target_image_ref}; {ended} before it noted the result",
                )
            return
        if info is not None and info.image_id == record.previous_image_id:
            if record.phase is UpdatePhase.ROLLING_BACK:
                self.finish(
                    record,
                    Outcome.ROLLED_BACK,
                    Failure.VERIFY_FAILED,
                    f"{ended}. The previous manager {record.previous_image_ref} runs again.",
                )
            else:
                self.finish(
                    record,
                    Outcome.FAILED,
                    Failure.HELPER_FAILED,
                    f"{ended}. The previous manager {record.previous_image_ref} still runs.",
                )
            return
        found = (
            "no manager container" if info is None else f"a manager container with the image {short_id(info.image_id)}"
        )
        self.finish(
            record,
            Outcome.FAILED,
            Failure.INTERNAL_ERROR,
            f"{ended}. Docker shows {found}, which is neither the old nor the new image. A person has to look at "
            "this host.",
        )

    def _seconds_since(self, stamp: str | None) -> float:
        if not stamp:
            return 0.0
        try:
            return max(0.0, (datetime.fromisoformat(self.now()) - datetime.fromisoformat(stamp)).total_seconds())
        except ValueError:
            return 0.0

    # ------------------------------------------------------------------ Clean up

    def _clean_up(self) -> None:
        """Remove what the finished updates leave without a purpose: the helper and an image. Once for each record.

        The helper still uses the old image, so the images go only after the helper is gone. After a swap that went
        well the old image is not needed any more. After a rollback it is the one that was pulled and did not run. After
        a failed rollback every image stays, because a person may need them to repair the host.
        """
        pending = [record for record in self.store.all() if record.finished and not record.cleaned_up]
        if not pending:
            return
        try:
            state = self.helper.state()
        except DockerError as exc:
            logger.debug("The clean-up of the self-update waits: %s", exc)
            return
        if state is not None and state.running:
            return
        if state is not None:
            self.helper.remove()
        for record in pending:
            self._clean_up_record(record)

    def _clean_up_record(self, record: SelfUpdate) -> None:
        """Clean up after one finished request. The record notes it first, so that a problem is not tried for ever."""
        record.cleaned_up = True
        self.store.save(record)
        removed = []
        try:
            if record.outcome is Outcome.SUCCEEDED:
                removed = self._remove_previous(record)
            elif record.outcome is Outcome.ROLLED_BACK and record.target_image_id:
                if self.docker.remove_image(record.target_image_id):
                    removed.append(short_id(record.target_image_id))
        except Exception:  # noqa: BLE001 - the update is over; a clean-up problem must not change its result
            logger.warning("Self-update %s: the clean-up failed; images may stay on the host", record.id, exc_info=True)
            return
        logger.info(
            "Self-update %s: clean-up finished, %s",
            record.id,
            f"removed {', '.join(removed)}" if removed else "nothing removed",
        )

    def _remove_previous(self, record: SelfUpdate) -> list[str]:
        """Remove the name ``<image>:previous`` and the image of the manager that ran before."""
        previous = record.previous_image_id
        if not previous or previous == record.target_image_id:
            return []
        existed = self.docker.image_id(previous) is not None
        name, _ = split_image_ref(record.previous_image_ref or "")
        if name and not (record.previous_image_ref or "").startswith("sha256:"):
            # The last name of the image goes with it, unless a container uses it.
            self.docker.remove_image(f"{name}:{PREVIOUS_TAG}")
        if self.docker.image_id(previous) is not None:
            self.docker.remove_image(previous)
        gone = existed and self.docker.image_id(previous) is None
        return [short_id(previous)] if gone else []
