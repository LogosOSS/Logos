"""Central node model configuration: what an admin sets per node and model."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

SYNC_NODE_CONFIG_ACTION = "sync_node_config"

_KV_SIZE = re.compile(r"^\d+(\.\d+)?[KMG]?$", re.IGNORECASE)
_GPU_DEVICES = re.compile(r"^(all|\d+(\s*,\s*\d+)*)?$", re.IGNORECASE)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# capability key -> (check, what the error message expects)
CAPABILITY_CHECKS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "tensor_parallel_size": (lambda v: _is_int(v) and v >= 1, "an integer >= 1"),
    "kv_cache_memory_bytes": (lambda v: isinstance(v, str) and bool(_KV_SIZE.match(v.strip())), 'a size like "8G"'),
    "gpu_devices": (lambda v: isinstance(v, str) and bool(_GPU_DEVICES.match(v.strip())), 'a list like "0,1"'),
    "base_residency_mb": (lambda v: _is_number(v) and v > 0, "a number > 0"),
    "min_context_fraction": (lambda v: _is_number(v) and 0 <= v <= 1, "a number from 0 to 1"),
}

# Not vLLM engine fields: the worker would route them out of overrides.
CAPABILITY_ONLY_KEYS = frozenset({"gpu_devices", "base_residency_mb", "min_context_fraction"})


def conflicts(capability: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Keys set in both places to different values."""
    return {
        key: (capability[key], overrides[key])
        for key in capability.keys() & overrides.keys()
        if capability[key] != overrides[key]
    }


def name_errors(entries: Iterable[Any]) -> list[str]:
    """Blank or repeated model names, which the table cannot hold."""
    errors: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.model_name.strip()
        if not name:
            errors.append("model_name must not be blank")
        elif name in seen:
            errors.append(f"{name}: listed twice")
        seen.add(name)
    return errors


def entry_errors(entries: Iterable[Any]) -> list[str]:
    """Problems that make a list unacceptable; empty when it is fine."""
    entries = list(entries)
    errors = name_errors(entries)
    for entry in entries:
        label = entry.model_name.strip() or "<blank>"
        for key, value in entry.capability.items():
            check = CAPABILITY_CHECKS.get(key)
            if check is None:
                errors.append(f"{label}: capability key '{key}' is not supported")
            elif not check[0](value):
                errors.append(f"{label}: capability '{key}' must be {check[1]}")
        for key in sorted(CAPABILITY_ONLY_KEYS & entry.overrides.keys()):
            errors.append(f"{label}: '{key}' belongs in capability, not overrides")
        for key, (cap, ovr) in sorted(conflicts(entry.capability, entry.overrides).items()):
            errors.append(f"{label}: '{key}' differs between capability ({cap!r}) and overrides ({ovr!r})")
    return errors


def conflict_warnings(entries: Iterable[Any]) -> list[str]:
    """Conflicts of a YAML import, which is stored as given."""
    return [
        f"{entry.model_name}: '{key}' differs between capability ({cap!r}) and overrides ({ovr!r})"
        for entry in entries
        for key, (cap, ovr) in sorted(conflicts(entry.capability, entry.overrides).items())
    ]
