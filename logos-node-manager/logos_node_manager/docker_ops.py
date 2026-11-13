"""The Docker operations of a deployment, as typed calls.

Compose stays the one place that defines the worker container (GPUs, volumes, limits), so the manager recreates the
container with ``docker compose up`` and never rebuilds that definition through the Docker API.

Two rules hold for everything in this module:

* ``docker compose config`` prints the resolved environment, secrets included. It is only called with ``--images``
  and ``--hash``, which print image names and a hash, and no output of it is logged.
* The requested image tag reaches Compose as the ``IMAGE_TAG`` process variable. A variable in the process environment
  beats the same variable in the ``.env`` file, so a later rewrite of that file cannot change what an earlier request
  deploys.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from logos_node_manager.config import Settings
from logos_node_manager.errors import DockerError
from logos_node_manager.runner import CommandResult, Runner

logger = logging.getLogger(__name__)

# Seconds allowed for the quick engine queries: inspect, tag, remove, logs, version.
QUERY_TIMEOUT_S = 30.0
# Compose has to read and interpolate the whole file before it prints the image names.
CONFIG_TIMEOUT_S = 60.0
# The labels on which Compose keeps what a container was made from: the hash of its configuration, the project and the
# service it belongs to, and the compose files it was made from.
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"
# The registry and namespace that the images come from when the ``.env`` file names none. It is the public mirror.
DEFAULT_REGISTRY = "ghcr.io/logososs"


@dataclass(frozen=True)
class ContainerInfo:
    """The part of ``docker inspect`` that a deployment looks at."""

    id: str
    image_id: str
    image_ref: str
    status: str
    running: bool
    restarting: bool
    exit_code: int
    restart_count: int
    # None when the image defines no health check.
    health: str | None
    # The configuration hash that Compose put on the container when it made it, or None if there is none.
    config_hash: str | None = None
    # What Compose says about the container: the project and the service it belongs to, and the compose files (a comma
    # separated list of paths) that it was made from. None for a container that Compose did not make.
    project: str | None = None
    service: str | None = None
    config_files: str | None = None


def split_image_ref(ref: str) -> tuple[str, str | None]:
    """Split ``registry:port/name:tag`` into the name and the tag. A digest is dropped.

    The colon of a registry port is not a tag separator: a tag colon comes after the last slash.
    """
    name = ref.split("@", 1)[0]
    colon = name.rfind(":")
    if colon > name.rfind("/"):
        return name[:colon], name[colon + 1 :]
    return name, None


def _container_from_inspect(raw: dict) -> ContainerInfo:
    state = raw.get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    config = raw.get("Config") or {}
    labels = config.get("Labels") or {}
    return ContainerInfo(
        id=str(raw.get("Id") or ""),
        image_id=str(raw.get("Image") or ""),
        image_ref=str(config.get("Image") or ""),
        status=str(state.get("Status") or ""),
        running=bool(state.get("Running")),
        restarting=bool(state.get("Restarting")),
        exit_code=int(state.get("ExitCode") or 0),
        restart_count=int(raw.get("RestartCount") or 0),
        health=str(health) if health else None,
        config_hash=str(labels[CONFIG_HASH_LABEL]) if labels.get(CONFIG_HASH_LABEL) else None,
        project=str(labels[PROJECT_LABEL]) if labels.get(PROJECT_LABEL) else None,
        service=str(labels[SERVICE_LABEL]) if labels.get(SERVICE_LABEL) else None,
        config_files=str(labels[CONFIG_FILES_LABEL]) if labels.get(CONFIG_FILES_LABEL) else None,
    )


def describe_registry_login(config_file: Path) -> str:
    """One line about the registry login the manager can see. Only names and counts, never a credential."""
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return f"no Docker login file at {config_file}"
    except (OSError, ValueError) as exc:
        return f"Docker login file {config_file} is unreadable: {exc}"
    registries = len(data.get("auths") or {})
    helper = data.get("credsStore") or ""
    helpers = len(data.get("credHelpers") or {})
    note = ""
    if helper or helpers:
        note = " (a credential helper is set; the helper program is not available in this container)"
    return (
        f"Docker login file: {registries} registry login(s), credsStore={helper or 'none'}, credHelpers={helpers}{note}"
    )


class Docker:
    """The docker command line, aimed at the worker service of one compose project.

    ``all_profiles`` makes Compose see the services that belong to a profile. The service of the manager itself has one,
    and the ``.env`` file of a host may not name it.
    """

    def __init__(self, settings: Settings, runner: Runner, *, all_profiles: bool = False) -> None:
        self._settings = settings
        self._runner = runner
        self._all_profiles = all_profiles
        # The compose file the commands use. None means the one of the host, ``settings.compose_file``.
        self._compose_file: Path | None = None

    # ------------------------------------------------------------------ Compose

    def use_compose(self, path: Path | None) -> None:
        """Aim the compose commands at another compose file. ``None`` goes back to the compose file of the host."""
        self._compose_file = path

    def _compose(self, *args: str) -> list[str]:
        s = self._settings
        if self._compose_file is None:
            files = ["-f", str(s.compose_file)]
        else:
            # The file lies outside the project directory, but a relative path in it, for example the bind mounts of the
            # worker, means the project directory. Compose takes the directory of the file unless it is told.
            files = ["--project-directory", str(s.project_dir), "-f", str(self._compose_file)]
        return ["docker", "compose", "-p", s.project_name, *files, "--env-file", str(s.env_file), *args]

    def _tag_env(self, tag: str | None) -> dict[str, str] | None:
        env = {"IMAGE_TAG": tag} if tag else {}
        if self._all_profiles:
            # A variable of the process beats the one in the .env file, so this holds whatever the file says.
            env["COMPOSE_PROFILES"] = "*"
        return env or None

    def validate(self, tag: str) -> CommandResult:
        """Check that Compose accepts the compose file together with the ``.env`` file of the host.

        ``-q`` prints nothing, so no value of the configuration reaches the output. A missing variable that the file
        requires, or an error in the file, ends up in the error text.
        """
        return self._runner.run(self._compose("config", "-q"), timeout_s=CONFIG_TIMEOUT_S, env=self._tag_env(tag))

    def pull(self, tag: str) -> CommandResult:
        """Download the image of the worker service for ``tag``."""
        return self._runner.run(
            self._compose("pull", self._settings.service),
            timeout_s=self._settings.pull_timeout_s,
            env=self._tag_env(tag),
        )

    def up(self, tag: str, *, recreate: bool = False, pull_never: bool = False) -> CommandResult:
        """Create or replace the worker container.

        Only the worker service is touched (``--no-deps``). Compose recreates the container when its image or its
        configuration changed, and leaves it alone otherwise.
        """
        args = ["up", "-d", "--no-deps"]
        if recreate:
            args.append("--force-recreate")
        if pull_never:
            args += ["--pull", "never"]
        args.append(self._settings.service)
        return self._runner.run(self._compose(*args), timeout_s=self._settings.swap_timeout_s, env=self._tag_env(tag))

    def image_ref(self, tag: str) -> str:
        """The image reference Compose uses for the worker service when the tag is ``tag``."""
        result = self._runner.run(
            self._compose("config", "--images", self._settings.service),
            timeout_s=CONFIG_TIMEOUT_S,
            env=self._tag_env(tag),
        )
        if not result.ok:
            raise DockerError(f"docker compose config failed: {result.tail()}")
        refs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(refs) != 1:
            raise DockerError(f"expected one image for service {self._settings.service!r}, got {len(refs)}")
        return refs[0]

    def config_hash(self, tag: str) -> str | None:
        """The hash of the configuration that Compose would make the worker container from now, or None if unknown.

        Compose recreates a container when its image or this hash changed. The hash changes with a variable of the
        ``.env`` file, with the compose file and with the image name, also when the image itself is the same. Only the
        hash is read, never the configuration.
        """
        result = self._runner.run(
            self._compose("config", "--hash", self._settings.service),
            timeout_s=CONFIG_TIMEOUT_S,
            env=self._tag_env(tag),
        )
        parts = result.stdout.split() if result.ok else []
        return parts[1] if len(parts) == 2 and parts[0] == self._settings.service else None

    # ------------------------------------------------------------------ Engine

    def container(self) -> ContainerInfo | None:
        """The worker container, or None if it does not exist."""
        result = self._runner.run(
            ["docker", "inspect", "--type", "container", self._settings.container], timeout_s=QUERY_TIMEOUT_S
        )
        if not result.ok:
            if "no such" in result.stderr.lower():
                return None
            raise DockerError(f"docker inspect failed: {result.tail()}")
        try:
            return _container_from_inspect(json.loads(result.stdout)[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise DockerError(f"docker inspect returned unexpected output: {exc}") from exc

    def image_id(self, ref: str) -> str | None:
        """The ID of a local image, or None if there is no such image."""
        result = self._runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], timeout_s=QUERY_TIMEOUT_S)
        if not result.ok:
            if "no such" in result.stderr.lower():
                return None
            raise DockerError(f"docker image inspect failed: {result.tail()}")
        return result.stdout.strip() or None

    def tag(self, source: str, target: str) -> None:
        """Give a local image another name."""
        result = self._runner.run(["docker", "tag", source, target], timeout_s=QUERY_TIMEOUT_S)
        if not result.ok:
            raise DockerError(f"docker tag failed: {result.tail()}")

    def remove_image(self, image_id: str) -> bool:
        """Delete a local image. Best effort: an image that a container still uses stays."""
        result = self._runner.run(["docker", "rmi", image_id], timeout_s=QUERY_TIMEOUT_S)
        if not result.ok:
            logger.info("Image %s was not removed: %s", image_id, result.tail(2))
        return result.ok

    def container_logs(self, lines: int = 40) -> str:
        """The end of the worker container's log, or an empty string if there is none."""
        result = self._runner.run(
            ["docker", "logs", "--tail", str(lines), self._settings.container], timeout_s=QUERY_TIMEOUT_S
        )
        return "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)

    def versions(self) -> str:
        """Engine and Compose versions, for the log at start. Never raises."""
        engine = self._runner.run(
            ["docker", "version", "--format", "engine {{.Server.Version}} (API {{.Server.APIVersion}})"],
            timeout_s=QUERY_TIMEOUT_S,
        )
        compose = self._runner.run(["docker", "compose", "version", "--short"], timeout_s=QUERY_TIMEOUT_S)
        engine_text = engine.stdout.strip() if engine.ok else f"engine unavailable ({engine.tail(1)})"
        compose_text = f"compose {compose.stdout.strip()}" if compose.ok else f"compose unavailable ({compose.tail(1)})"
        return f"{engine_text}, {compose_text}"


def self_docker(settings: Settings, runner: Runner) -> Docker:
    """The docker command line, aimed at the service and the container of the manager itself.

    The calls that a deployment makes for the worker service (pull, up, the image name, inspect) then work for the
    manager as well, with the project and the compose file of the manager.
    """
    own = replace(
        settings,
        project_name=settings.self_project_name,
        compose_file=settings.self_compose_file,
        service=settings.self_service,
        container=settings.self_container,
    )
    return Docker(own, runner, all_profiles=True)


# ---------------------------------------------------------------------- A whole compose project (core host)

ONE_OFF_LABEL = "com.docker.compose.oneoff"


@dataclass(frozen=True)
class ProjectContainer:
    """A container of the compose project, with the service it belongs to."""

    service: str
    info: ContainerInfo


def read_env_value(path: Path, name: str) -> str | None:
    """The value of ``name`` in a ``.env`` file, or None. The last assignment wins, as it does for Compose.

    A value in double quotes is read as JSON, which is how the deploy workflows write it. Anything else is taken as it
    stands, without surrounding quotes. The file is read, never run: a value is data, not a shell command.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found: str | None = None
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = str(json.loads(value))
            except ValueError:
                value = value[1:-1]
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        found = value
    return found


class ProjectDocker(Docker):
    """The docker command line, aimed at the whole compose project of a core host.

    The core is many services, not one container: a deployment pulls the images of all of them and lets Compose replace
    the ones that changed. Nothing here removes an image or a container by name, apart from what Compose itself does
    (``--remove-orphans``) and the prune of images no container uses.
    """

    def pull_project(self, tag: str) -> CommandResult:
        """Download the images of every service of the project for ``tag``."""
        return self._runner.run(self._compose("pull"), timeout_s=self._settings.pull_timeout_s, env=self._tag_env(tag))

    def up_project(self, tag: str, replicas: int) -> CommandResult:
        """Create or replace the services whose image or configuration changed, and remove the ones that are gone."""
        args = ["up", "-d", "--remove-orphans"]
        if self._settings.scale_service:
            args += ["--scale", f"{self._settings.scale_service}={replicas}"]
        return self._runner.run(self._compose(*args), timeout_s=self._settings.swap_timeout_s, env=self._tag_env(tag))

    def replicas(self) -> int:
        """How many copies of the scaled service the ``.env`` file asks for. One if it is missing or not a number."""
        raw = read_env_value(self._settings.env_file, self._settings.replicas_variable)
        if raw is None or not raw.strip():
            return 1
        try:
            count = int(raw.strip())
        except ValueError:
            logger.warning("%s is not a whole number; using 1", self._settings.replicas_variable)
            return 1
        if count < 1:
            logger.warning("%s must be at least 1; using 1", self._settings.replicas_variable)
            return 1
        return count

    def services(self) -> list[str]:
        """The services of the project that should run: those of the active profiles."""
        result = self._runner.run(self._compose("config", "--services"), timeout_s=CONFIG_TIMEOUT_S)
        if not result.ok:
            raise DockerError(f"docker compose config failed: {result.tail()}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def project_containers(self) -> list[ProjectContainer]:
        """Every container of the project, stopped ones included. One-off containers (``compose run``) do not count."""
        listing = self._runner.run(
            [
                "docker",
                "ps",
                "-a",
                "-q",
                "--no-trunc",
                "--filter",
                f"label={PROJECT_LABEL}={self._settings.project_name}",
            ],
            timeout_s=QUERY_TIMEOUT_S,
        )
        if not listing.ok:
            raise DockerError(f"docker ps failed: {listing.tail()}")
        ids = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        if not ids:
            return []
        inspected = self._runner.run(["docker", "inspect", "--type", "container", *ids], timeout_s=QUERY_TIMEOUT_S)
        if not inspected.ok:
            raise DockerError(f"docker inspect failed: {inspected.tail()}")
        try:
            raw_containers = json.loads(inspected.stdout)
            found = []
            for raw in raw_containers:
                labels = (raw.get("Config") or {}).get("Labels") or {}
                if labels.get(ONE_OFF_LABEL) == "True":
                    continue
                found.append(
                    ProjectContainer(service=str(labels.get(SERVICE_LABEL) or ""), info=_container_from_inspect(raw))
                )
        except (ValueError, TypeError, AttributeError) as exc:
            raise DockerError(f"docker inspect returned unexpected output: {exc}") from exc
        return found

    def service_logs(self, services: list[str], lines: int = 30) -> str:
        """The end of the log of some services, or an empty string if there is none."""
        if not services:
            return ""
        result = self._runner.run(
            self._compose("logs", "--no-color", "--tail", str(lines), *services), timeout_s=QUERY_TIMEOUT_S
        )
        return "\n".join(part for part in (result.stdout.rstrip(), result.stderr.rstrip()) if part)

    def prune_images(self) -> CommandResult:
        """Remove every image that no container uses."""
        return self._runner.run(["docker", "image", "prune", "-af"], timeout_s=self._settings.pull_timeout_s)

    def pull_image(self, reference: str) -> CommandResult:
        """Download one image by name. For an image that is not a service of the project."""
        return self._runner.run(["docker", "pull", reference], timeout_s=self._settings.pull_timeout_s)
