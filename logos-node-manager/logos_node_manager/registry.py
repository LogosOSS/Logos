"""The registry login of the host, made by the manager from the ``.env`` file.

A host pulls its images from a registry that wants a login. Somebody had to run ``docker login`` on every host, once,
and again whenever the password changed. Now the manager does it: before a deployment pulls, it reads the user and the
password of the registry from the ``.env`` file of the host and logs in.

* ``REGISTRY`` is the registry and the namespace the images come from, as the compose files use it. The host name is its
  part before the first slash.
* ``REGISTRY_USER`` and ``REGISTRY_PASSWORD`` are the login. An account that may only pull is enough. Without both, the
  manager does not log in, and the login that the host already has (the directory ``DOCKER_CONFIG``) is used as before.

The password goes to ``docker login`` through stdin, so it is on no command line and in no log. The login is stored in a
directory of its own in the temporary file system of the container: it is gone when the container stops, and nothing of
it is written to the host. The ``.env`` file is read at every login, so a new password needs no restart.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from logos_node_manager.config import Settings
from logos_node_manager.docker_ops import DEFAULT_REGISTRY, read_env_value
from logos_node_manager.runner import Runner

logger = logging.getLogger(__name__)

REGISTRY_NAME = "REGISTRY"
USER_NAME = "REGISTRY_USER"
PASSWORD_NAME = "REGISTRY_PASSWORD"
# Seconds a login may take. It is one small request to the registry.
LOGIN_TIMEOUT_S = 60.0


def registry_host(registry: str | None) -> str:
    """The host, and the port if there is one, of a ``REGISTRY`` value such as ``harbor.example/logos``."""
    value = (registry or "").strip() or DEFAULT_REGISTRY
    return value.split("/", 1)[0]


class RegistryLogin:
    """Logs in to the registry of the host with the user and the password of its ``.env`` file."""

    def __init__(self, settings: Settings, runner: Runner, *, config_dir: Path | None = None) -> None:
        self._settings = settings
        self._runner = runner
        # Made on the first login when it is not given. ``DOCKER_CONFIG`` points there while a login is in use.
        self._config_dir = config_dir
        self._warned_about_half = False

    # ------------------------------------------------------------------ What the .env file says

    def _read(self) -> tuple[str, str, str]:
        """The registry host, the user and the password of the ``.env`` file. The last two are empty if not set."""
        env_file = self._settings.env_file
        host = registry_host(read_env_value(env_file, REGISTRY_NAME))
        user = (read_env_value(env_file, USER_NAME) or "").strip()
        password = read_env_value(env_file, PASSWORD_NAME) or ""
        return host, user, password

    def describe(self) -> str:
        """One line for the log at start. Never the password."""
        host, user, password = self._read()
        if user and password:
            return f"registry login from the .env file: user {user} at {host}; the manager logs in before it pulls"
        if user or password:
            return f"{USER_NAME} and {PASSWORD_NAME} must both be set in the .env file; the manager does not log in"
        return f"no registry login in the .env file ({USER_NAME}, {PASSWORD_NAME}): the login of the host is used"

    # ------------------------------------------------------------------ Login

    def login(self) -> str | None:
        """Log in if the ``.env`` file has a login. Returns None, or the reason why the login failed.

        Without a login in the file this does nothing, and the commands that follow use the login of the host. With
        one, they use the login made here. A failure is a text without the password, for the record of the deployment.
        """
        host, user, password = self._read()
        if not user and not password:
            self._use(None)
            return None
        if not user or not password:
            # One of the two is missing, most likely a typo. A host that has a working login of its own must not be
            # stopped by it, so the manager says so once and goes on without a login of its own.
            if not self._warned_about_half:
                self._warned_about_half = True
                logger.warning(
                    "%s and %s must both be set in the .env file; no registry login is made", USER_NAME, PASSWORD_NAME
                )
            self._use(None)
            return None

        directory = self._directory()
        result = self._runner.run(
            ["docker", "login", host, "--username", user, "--password-stdin"],
            timeout_s=LOGIN_TIMEOUT_S,
            env={"DOCKER_CONFIG": str(directory)},
            stdin=password,
        )
        if not result.ok:
            self._use(None)
            return f"the login to the registry {host} failed: {result.tail(4).replace(password, '***')}"
        self._use(directory)
        logger.info("Logged in to the registry %s as %s", host, user)
        return None

    def _directory(self) -> Path:
        if self._config_dir is None:
            self._config_dir = Path(tempfile.mkdtemp(prefix="nm-docker-login-"))
        self._config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._config_dir

    def _use(self, directory: Path | None) -> None:
        """Point the docker commands that follow at the login made here, or back at the login of the host."""
        set_env = getattr(self._runner, "set_env", None)
        if set_env is not None:
            set_env("DOCKER_CONFIG", str(directory) if directory else None)
