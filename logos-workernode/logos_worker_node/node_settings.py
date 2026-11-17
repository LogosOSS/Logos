"""Node-wide settings that the worker reads from Logos.

A settings document holds only the keys an admin set; a key it lacks leaves the
value of config.yml in force. Of these, the global vLLM flags reach every
plan, the GPU pool the next lane placement, and the GPU poll interval the next
start.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from logos_worker_node.models import AppConfig

logger = logging.getLogger(__name__)

SYNC_NODE_SETTINGS_ACTION = "sync_node_settings"
SETTING_KEYS = ("global_extra_args", "gpu_devices", "auto_reboot_on_stuck_gpu", "gpu_poll_interval")
# Settings that only the next worker start puts into effect.
RESTART_KEYS = frozenset({"gpu_poll_interval"})

_GPU_POOL = re.compile(r"^(all|none|\d+(,\d+)*)$")
_MAX_POLL_SECONDS = 300


def normalize_node_settings(body: Any) -> dict[str, Any]:
    """Logos' answer as {revision, yaml_imported, settings}; raises if unusable."""
    if not isinstance(body, dict) or not isinstance(body.get("settings"), dict):
        raise RuntimeError("Logos node-settings response has no settings mapping")
    return {
        "revision": int(body.get("revision") or 0),
        "yaml_imported": bool(body.get("yaml_imported")),
        "settings": {key: value for key, value in body["settings"].items() if key in SETTING_KEYS},
    }


def settings_from_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """The node-wide settings a config.yml states, exactly as written."""
    engines = raw.get("engines") if isinstance(raw.get("engines"), dict) else {}
    vllm = engines.get("vllm") if isinstance(engines.get("vllm"), dict) else {}
    worker = raw.get("worker") if isinstance(raw.get("worker"), dict) else {}
    settings: dict[str, Any] = {}
    if isinstance(vllm.get("global_extra_args"), list):
        settings["global_extra_args"] = [str(arg) for arg in vllm["global_extra_args"]]
    for key in ("gpu_devices", "auto_reboot_on_stuck_gpu", "gpu_poll_interval"):
        if key in worker:
            settings[key] = worker[key]
    return settings


def settings_from_yaml_file(config_path: Path) -> dict[str, Any]:
    with config_path.open() as f:
        return settings_from_yaml(yaml.safe_load(f) or {})


def usable(key: str, value: Any) -> bool:
    """Whether the worker can run with this value; a refused one is skipped, not fatal."""
    if key == "global_extra_args":
        return isinstance(value, list) and all(isinstance(arg, str) and arg.strip() for arg in value)
    if key == "gpu_devices":
        return isinstance(value, str) and bool(_GPU_POOL.match(value.strip()))
    if key == "auto_reboot_on_stuck_gpu":
        return isinstance(value, bool)
    if key == "gpu_poll_interval":
        return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_POLL_SECONDS
    return False


def apply_node_settings(cfg: AppConfig, settings: dict[str, Any], revision: int) -> list[str]:
    """Make ``settings`` the worker's, keeping config.yml's value for any key they lack.

    Returns the keys that changed a value. Values the worker cannot use are
    logged and left as they are.
    """
    changed = []
    for key in SETTING_KEYS:
        if key not in settings:
            continue
        value = settings[key]
        if not usable(key, value):
            logger.warning("[NodeSettings] Ignoring %s=%r: not a usable value", key, value)
            continue
        target = cfg.engines.vllm if key == "global_extra_args" else cfg.worker
        new = [str(arg) for arg in value] if key == "global_extra_args" else value
        if getattr(target, key) != new:
            setattr(target, key, new)
            changed.append(key)
    cfg.logos.node_settings_revision = revision
    return changed
