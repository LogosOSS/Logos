"""The compose file of a deployment, taken out of the image of its tag.

A compose file says which containers run and with which settings. Until now every host had a copy of it, and a person
or a workflow had to replace the copy whenever the file changed. A release that needs a new entry then ran with the old
settings. With this module the file travels with the tag: the image ``logos-node-manager:<tag>`` carries the compose
files of the commit it was built from, and the manager takes the one of its host out of that image before a deployment
pulls anything. The settings and the images of a deployment are then always of the same version.

Taking a file out of an image does not start the image. The manager creates a container that it never starts, copies
the file out of it and removes the container.

* The switch is ``NODE_MANAGER_COMPOSE_FROM_TAG`` in the ``.env`` file of the host. It is off by default, and a change
  needs no restart: the file is read at the start of every deployment.
* An image without the compose files, for example the image of an older tag, is not an error. The deployment uses the
  compose file of the host, as it did before, and the record says so.
* Everything else that goes wrong is an error, and the deployment ends as failed before it touches anything.

The file is kept in the state directory, one for each deployment, because a restart of the manager must continue with
the same file, also if the tag has moved on in the meantime. ``.current`` holds the file that ran last, which the
rollback of a worker needs: it restores the previous image together with the previous settings.

The self-update of the manager takes the file that defines the manager's own service out of the same image. On a
worker host the manager is a service of the compose file of the worker, so it is the same file. On a core host the
manager is a project of its own, and the image carries its file as ``deployer.yml``. An image of an older tag may not
have it. That is no error either: the update then uses the compose file of the host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from logos_node_manager.config import CORE, WORKER, Settings
from logos_node_manager.docker_ops import DEFAULT_REGISTRY, QUERY_TIMEOUT_S, read_env_value
from logos_node_manager.errors import ComposeError
from logos_node_manager.runner import CommandResult, Runner
from logos_node_manager.state import Deployment

logger = logging.getLogger(__name__)

SWITCH_NAME = "NODE_MANAGER_COMPOSE_FROM_TAG"
# The image that carries the compose files is the image of the node manager: the manager pulls it anyway, and the
# build makes it from the same commit as the other images of the tag.
CARRIER_IMAGE = "logos-node-manager"
# The label of an image that carries compose files. Its value is the version of the layout, which is 1.
BUNDLE_LABEL = "io.logos.compose-bundle"
BUNDLE_VERSION = "1"
# The directory in the image that holds the files, and the name of the file for each kind of host.
BUNDLE_DIR = "/compose"
FILE_NAMES = {CORE: "core.yaml", WORKER: "worker.yml"}
# The file that defines the manager's own service, for the self-update.
SELF_FILE_NAMES = {CORE: "deployer.yml", WORKER: "worker.yml"}
# A compose file of Logos is some tens of kilobytes. A file far above that is not one.
MAX_BYTES = 1024 * 1024
CURRENT = ".current"
_TRUE = {"1", "true", "yes", "on"}
# What Docker says when a tag does not exist in the registry. The classic image store says "manifest unknown" or
# "manifest for <image> not found". The containerd image store, which is the default of newer engines and of Docker
# Desktop, says "failed to resolve reference "<image>": <image>: not found". A plain "not found" is no sign of either:
# it is also what a missing ``docker`` program says. An authorization error never ends in "not found", so it stays an
# error.
_MISSING_TAG = re.compile(r"manifest unknown|manifest for \S+ not found|failed to resolve reference [^\n]*: not found")


@dataclass(frozen=True)
class FetchedCompose:
    """A compose file that was taken out of an image."""

    path: Path
    sha256: str
    image: str


class _Subject(Protocol):
    """What a fetch needs to know about the request it is for: its id, which names the files, and its tag."""

    id: str
    tag: str


def _missing(result: CommandResult) -> bool:
    """Whether a failed pull says that the tag does not exist, as against a registry or network that did not answer."""
    return bool(_MISSING_TAG.search(f"{result.stderr}\n{result.stdout}".lower()))


def _file_missing(result: CommandResult) -> bool:
    """Whether a failed copy says that the file is not in the image."""
    text = f"{result.stderr}\n{result.stdout}".lower()
    return "could not find the file" in text or "no such file or directory" in text


class ComposeSource:
    """Takes the compose file of the host out of the image of a tag, and remembers the one that ran last."""

    def __init__(self, settings: Settings, runner: Runner, compose_dir: Path) -> None:
        self._settings = settings
        self._runner = runner
        self._root = compose_dir

    # ------------------------------------------------------------------ What the .env file says

    def enabled(self) -> bool:
        """Whether the compose file comes from the tag. Read from the ``.env`` file each time it is asked."""
        raw = read_env_value(self._settings.env_file, SWITCH_NAME)
        return bool(raw) and raw.strip().lower() in _TRUE

    def describe(self) -> str:
        """One line for the log at start."""
        if self.enabled():
            name = FILE_NAMES[self._settings.kind]
            return f"the compose file comes from the image of the tag ({CARRIER_IMAGE}:<tag>, the file {name})"
        return f"the compose file of the host is used ({SWITCH_NAME} is off in the .env file)"

    def carrier_reference(self, tag: str) -> str:
        registry = (read_env_value(self._settings.env_file, "REGISTRY") or "").strip() or DEFAULT_REGISTRY
        return f"{registry}/{CARRIER_IMAGE}:{tag}"

    # ------------------------------------------------------------------ Taking the file out of the image

    def fetch(self, deployment: _Subject, *, own: bool = False) -> FetchedCompose | None:
        """Take the compose file of this kind of host out of the image of the tag of the deployment.

        With ``own`` it is the file that defines the manager's own service, for a self-update. The request may be a
        deployment or a self-update: both have an id and a tag. A deployment and a self-update with the same id keep
        their files apart.

        Returns None if the image carries none. Raises ``ComposeError`` if the image cannot be had, or if what it
        carries cannot be used. Every step can be repeated: a container that an earlier attempt left behind is removed
        first, and the file is put in place in one step.
        """
        name = (SELF_FILE_NAMES if own else FILE_NAMES)[self._settings.kind]
        key = f"self-{deployment.id}" if own else deployment.id
        reference = self.carrier_reference(deployment.tag)

        pulled = self._runner.run(["docker", "pull", reference], timeout_s=self._settings.pull_timeout_s)
        if not pulled.ok:
            if _missing(pulled):
                logger.info("There is no image %s", reference)
                return None
            raise ComposeError(f"the image {reference} could not be downloaded: {pulled.tail(4)}")
        if not self._carries_compose_files(reference):
            return None

        directory = self._root / key
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        container = f"nm-compose-{key}"
        self._remove_container(container)
        created = self._runner.run(
            ["docker", "create", "--pull", "never", "--name", container, reference], timeout_s=QUERY_TIMEOUT_S
        )
        if not created.ok:
            raise ComposeError(
                f"a container of {reference} could not be made to read its compose file: {created.tail(4)}"
            )
        try:
            copied = self._runner.run(
                ["docker", "cp", f"{container}:{BUNDLE_DIR}/{name}", str(temporary)], timeout_s=QUERY_TIMEOUT_S
            )
            if not copied.ok:
                if own and _file_missing(copied):
                    # An image from before the file was added to it.
                    logger.info("The image %s carries no %s", reference, name)
                    return None
                raise ComposeError(f"{BUNDLE_DIR}/{name} could not be copied out of {reference}: {copied.tail(4)}")
            try:
                data = temporary.read_bytes()
            except OSError as exc:
                raise ComposeError(f"{BUNDLE_DIR}/{name} of {reference} could not be read: {exc}") from exc
            if not data or len(data) > MAX_BYTES:
                raise ComposeError(f"{BUNDLE_DIR}/{name} of {reference} is empty or too large ({len(data)} bytes)")
            final = directory / name
            os.replace(temporary, final)
        finally:
            self._remove_container(container)
            temporary.unlink(missing_ok=True)
        return FetchedCompose(path=final, sha256=hashlib.sha256(data).hexdigest(), image=reference)

    def _carries_compose_files(self, reference: str) -> bool:
        """Whether the image says that it carries compose files. Raises for a layout this manager does not know."""
        inspected = self._runner.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", reference], timeout_s=QUERY_TIMEOUT_S
        )
        if not inspected.ok:
            raise ComposeError(f"the image {reference} could not be inspected: {inspected.tail(4)}")
        try:
            labels = json.loads(inspected.stdout.strip() or "null") or {}
        except ValueError as exc:
            raise ComposeError(f"the labels of the image {reference} are unreadable: {exc}") from exc
        version = str(labels.get(BUNDLE_LABEL, ""))
        if not version:
            logger.info("The image %s carries no compose files", reference)
            return False
        if version != BUNDLE_VERSION:
            raise ComposeError(
                f"the image {reference} carries compose files of layout {version}, which this node manager does not "
                f"read (it reads layout {BUNDLE_VERSION}). Update the node manager of this host"
            )
        return True

    def _remove_container(self, name: str) -> None:
        """Remove the container that was made to read a file. A container that is not there is no problem."""
        self._runner.run(["docker", "rm", "-f", name], timeout_s=QUERY_TIMEOUT_S)

    # ------------------------------------------------------------------ The file that ran last

    def current_path(self) -> Path | None:
        """The compose file that ran last, or None if the compose file of the host ran last."""
        path = self._root / CURRENT / FILE_NAMES[self._settings.kind]
        return path if path.is_file() else None

    def remember(self, deployment: Deployment) -> None:
        """Note which compose file runs now: the one of the deployment, or the one of the host if it used none.

        This is what a rollback of the next deployment restores. It runs when a deployment ends well. Repeating it
        changes nothing.
        """
        directory = self._root / CURRENT
        if not deployment.compose_file:
            shutil.rmtree(directory, ignore_errors=True)
            return
        name = FILE_NAMES[self._settings.kind]
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        try:
            shutil.copyfile(deployment.compose_file, temporary)
            os.replace(temporary, directory / name)
        finally:
            temporary.unlink(missing_ok=True)
