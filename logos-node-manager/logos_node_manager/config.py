"""Settings of the node manager.

Everything comes from environment variables with the prefix ``NODE_MANAGER_``. The defaults match the layout the
deploy workflows create on a worker host: the compose project in ``/opt/logos-workernode`` with the worker service
and container ``logos-workernode``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PREFIX = "NODE_MANAGER_"

_TRUE = {"1", "true", "yes", "on"}


def _seconds(environ: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False) -> float:
    """A number of seconds from the environment.

    A missing, malformed or out-of-range value falls back to ``default``: a bad override must neither stop the manager
    at start nor silently switch a timeout off.
    """
    raw = environ.get(PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value > 0 or (allow_zero and value == 0):
        return value
    return default


def _text(environ: Mapping[str, str], name: str, default: str) -> str:
    raw = environ.get(PREFIX + name)
    return raw.strip() if raw and raw.strip() else default


def _flag(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(PREFIX + name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUE


def compose_project_name(project_dir: Path) -> str:
    """The project name Compose derives from the directory of the compose file.

    The manager passes it explicitly, so it finds the containers that a plain ``docker compose`` on the host created.
    """
    name = re.sub(r"[^a-z0-9_-]", "", project_dir.name.lower())
    return name or "logos-workernode"


WORKER = "worker"
CORE = "core"


@dataclass(frozen=True)
class Settings:
    """What the manager needs to know about its host."""

    # The compose project of the worker. The directory is mounted at the same path as on the host, because Compose
    # turns relative bind mounts into absolute host paths before it sends them to the Docker engine.
    project_dir: Path
    compose_file: Path
    env_file: Path
    project_name: str
    service: str
    container: str
    state_dir: Path

    # Timeouts in seconds.
    pull_timeout_s: float
    swap_timeout_s: float
    verify_timeout_s: float
    rollback_verify_timeout_s: float
    # How long the container must stay healthy, without a restart, before a deployment counts as done.
    stability_window_s: float
    poll_interval_s: float
    loop_interval_s: float

    # Whether a deployment drains the node before it replaces the container. Off switches the whole drain phase off,
    # so it is also the way to deploy as before if the orchestrator misbehaves.
    drain_enabled: bool
    # The orchestrator cordons the node and says when its requests are done. The node authenticates with the key of the
    # worker, so it can only cordon itself. Without both values a deployment cannot drain and swaps at once.
    orchestrator_url: str
    node_key: str = field(repr=False)
    # Time limits of the drain, in seconds. The drain limit starts when the cordon is granted. The turn limit covers the
    # wait for the cordon while another node is being replaced. The retry window is how long an unreachable
    # orchestrator is asked again before the drain is skipped. The reconnect limit is how long the replaced worker may
    # take to show up at the orchestrator.
    drain_timeout_s: float
    turn_timeout_s: float
    orchestrator_retry_window_s: float
    orchestrator_timeout_s: float
    reconnect_timeout_s: float
    log_level: str

    # What the host is: a GPU worker (``worker``) or the core of Logos (``core``). A worker host replaces the one worker
    # container. A core host replaces the services of the whole compose project, and the manager sits outside that
    # project so that it survives the replacement of Logos.
    kind: str = WORKER
    # The Logos that this host obeys: its address, and the key that Logos knows the host by. A core host has a deployer
    # key that this Logos made for it. A worker is known by its worker key, the key that it also uses with the
    # orchestrator. It has no deployer key, and a DEPLOYER_KEY on a worker is not read. The manager asks Logos for
    # orders and tells it how they go. A host obeys one Logos only. Without both values, only the command line gives
    # orders.
    logos_url: str = ""
    deployer_key: str = field(default="", repr=False)
    # How long a request for orders may wait at Logos before it answers with none, and the margin that the connection
    # gets on top of it.
    order_wait_s: float = 25.0
    order_timeout_margin_s: float = 15.0
    # How often a deployment in progress is reported to Logos, also when nothing changed. Logos reads the report as a
    # sign that the host is alive.
    report_interval_s: float = 20.0

    # Core host only. The service that runs as several copies, the variable of the ``.env`` file that says how many, the
    # time the services get to come up, and the address that must answer once they did
    # (empty: not checked, which is the case without a Logos).
    scale_service: str = "logos-webservice"
    replicas_variable: str = "LOGOS_WEBSERVICE_REPLICAS"
    core_verify_timeout_s: float = 900.0
    core_health_url: str = ""

    # The manager's own service, which a self-update replaces. A worker host has the manager next to the worker, in the
    # compose file of the worker. A core host has it in a compose project of its own, ``logos-deployer``, so that a
    # deployment of Logos never replaces it. The compose file of that project lies next to the one of Logos.
    self_project_name: str = ""
    self_compose_file: Path = Path()
    self_service: str = ""
    self_container: str = ""
    # How long the new manager gets to become healthy after the swap, in seconds. Its health check runs every 30 s, and
    # the first run comes one interval after the start.
    self_verify_timeout_s: float = 180.0

    @property
    def logos_enabled(self) -> bool:
        """Whether this host takes orders from a Logos: it needs the address and the key."""
        return bool(self.logos_url and self.deployer_key)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        kind = _text(env, "KIND", WORKER).lower()
        if kind not in (WORKER, CORE):
            kind = WORKER
        core = kind == CORE
        project_dir = Path(_text(env, "PROJECT_DIR", "/opt/logos" if core else "/opt/logos-workernode"))
        compose_name = "docker-compose.yaml" if core else "docker-compose.yml"
        # An address that is not http(s) counts as none: a bad value must not stop the manager at start.
        logos_url = _text(env, "LOGOS_URL", "").rstrip("/")
        if not logos_url.startswith(("https://", "http://")):
            logos_url = ""
        node_key = _text(env, "NODE_KEY", "")
        # Logos knows a worker by its worker key and a core host by its deployer key. A host reads only its own key. A
        # worker has no deployer key, so DEPLOYER_KEY means nothing on a worker. A core host has no worker key.
        deployer_key = _text(env, "DEPLOYER_KEY", "") if core else node_key
        health_default = f"{logos_url}/api/info" if core and logos_url and deployer_key else ""
        project_name = _text(env, "PROJECT_NAME", compose_project_name(project_dir))
        compose_file = Path(_text(env, "COMPOSE_FILE", str(project_dir / compose_name)))
        # The names of the manager's own service. They match the compose files of this repository.
        self_name = "logos-deployer" if core else "logos-node-manager"
        self_compose_default = project_dir / "docker-compose.deployer.yml" if core else compose_file
        return cls(
            kind=kind,
            logos_url=logos_url,
            deployer_key=deployer_key,
            order_wait_s=_seconds(env, "ORDER_WAIT_S", 25.0),
            order_timeout_margin_s=_seconds(env, "ORDER_TIMEOUT_MARGIN_S", 15.0),
            report_interval_s=_seconds(env, "REPORT_INTERVAL_S", 20.0),
            scale_service=_text(env, "SCALE_SERVICE", "logos-webservice"),
            replicas_variable=_text(env, "REPLICAS_VARIABLE", "LOGOS_WEBSERVICE_REPLICAS"),
            core_verify_timeout_s=_seconds(env, "CORE_VERIFY_TIMEOUT_S", 900.0),
            core_health_url=_text(env, "CORE_HEALTH_URL", health_default),
            self_project_name=_text(env, "SELF_PROJECT_NAME", self_name if core else project_name),
            self_compose_file=Path(_text(env, "SELF_COMPOSE_FILE", str(self_compose_default))),
            self_service=_text(env, "SELF_SERVICE", self_name),
            self_container=_text(env, "SELF_CONTAINER", self_name),
            self_verify_timeout_s=_seconds(env, "SELF_VERIFY_TIMEOUT_S", 180.0),
            project_dir=project_dir,
            compose_file=compose_file,
            env_file=Path(_text(env, "ENV_FILE", str(project_dir / ".env"))),
            project_name=project_name,
            service=_text(env, "SERVICE", "logos-workernode"),
            container=_text(env, "CONTAINER", "logos-workernode"),
            state_dir=Path(_text(env, "STATE_DIR", str(project_dir / "node-manager"))),
            pull_timeout_s=_seconds(env, "PULL_TIMEOUT_S", 1800.0),
            swap_timeout_s=_seconds(env, "SWAP_TIMEOUT_S", 300.0),
            verify_timeout_s=_seconds(env, "VERIFY_TIMEOUT_S", 600.0),
            rollback_verify_timeout_s=_seconds(env, "ROLLBACK_VERIFY_TIMEOUT_S", 300.0),
            stability_window_s=_seconds(env, "STABILITY_WINDOW_S", 60.0, allow_zero=True),
            poll_interval_s=_seconds(env, "POLL_INTERVAL_S", 5.0),
            loop_interval_s=_seconds(env, "LOOP_INTERVAL_S", 2.0),
            drain_enabled=_flag(env, "DRAIN_ENABLED", False),
            orchestrator_url=_text(env, "ORCHESTRATOR_URL", "").rstrip("/"),
            node_key=node_key,
            drain_timeout_s=_seconds(env, "DRAIN_TIMEOUT_S", 900.0),
            turn_timeout_s=_seconds(env, "TURN_TIMEOUT_S", 7200.0),
            orchestrator_retry_window_s=_seconds(env, "ORCHESTRATOR_RETRY_WINDOW_S", 120.0),
            orchestrator_timeout_s=_seconds(env, "ORCHESTRATOR_TIMEOUT_S", 15.0),
            reconnect_timeout_s=_seconds(env, "RECONNECT_TIMEOUT_S", 300.0),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        )
