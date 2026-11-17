"""Settings of a whole node that belong to no model, and how they are checked.

A settings document holds only the keys an admin set; a missing key leaves the
worker's config.yml value in force. The worker applies the same rules.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

SYNC_NODE_SETTINGS_ACTION = "sync_node_settings"

_GPU_POOL = re.compile(r"^(all|none|\d+(,\d+)*)$")
_ARG_LIMIT = 200
_MAX_POLL_SECONDS = 300

SETTING_KEYS = ("global_extra_args", "gpu_devices", "auto_reboot_on_stuck_gpu", "gpu_poll_interval")


def settings_errors(settings: Mapping[str, Any]) -> list[str]:
    """Problems that stop a save, each as "<key>: <message>"."""
    errors = []
    for key in settings:
        if key not in SETTING_KEYS:
            errors.append(f"{key}: unknown setting")
    args = settings.get("global_extra_args")
    if "global_extra_args" in settings:
        if not isinstance(args, list) or not all(isinstance(a, str) and a.strip() for a in args):
            errors.append("global_extra_args: must be a list of non-empty strings")
        elif len(args) > _ARG_LIMIT:
            errors.append(f"global_extra_args: at most {_ARG_LIMIT} arguments")
        elif any(not a.startswith("-") and not _continues_a_flag(args, i) for i, a in enumerate(args)):
            errors.append("global_extra_args: every argument starts with - (or is the value of the one before)")
    pool = settings.get("gpu_devices")
    if "gpu_devices" in settings and not (isinstance(pool, str) and _GPU_POOL.match(pool.strip())):
        errors.append('gpu_devices: use "all", "none" or GPU numbers like "0,1"')
    reboot = settings.get("auto_reboot_on_stuck_gpu")
    if "auto_reboot_on_stuck_gpu" in settings and not isinstance(reboot, bool):
        errors.append("auto_reboot_on_stuck_gpu: must be true or false")
    poll = settings.get("gpu_poll_interval")
    if "gpu_poll_interval" in settings and not (
        isinstance(poll, int) and not isinstance(poll, bool) and 1 <= poll <= _MAX_POLL_SECONDS
    ):
        errors.append(f"gpu_poll_interval: whole seconds from 1 to {_MAX_POLL_SECONDS}")
    return errors


def _continues_a_flag(args: list[str], index: int) -> bool:
    """Whether an argument without a dash is the value of the flag before it."""
    return index > 0 and args[index - 1].startswith("-") and "=" not in args[index - 1]


def error_items(errors: list[str]) -> list[dict[str, Any]]:
    """Errors as {key, message}, for a user interface that shows them at their fields."""
    items = []
    for error in errors:
        key, _, message = error.partition(": ")
        items.append({"key": key, "message": message or error})
    return items
