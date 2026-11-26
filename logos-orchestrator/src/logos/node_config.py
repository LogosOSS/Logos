"""Central node model configuration: what an admin sets per node and model."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

SYNC_NODE_CONFIG_ACTION = "sync_node_config"
VALIDATE_NODE_CONFIG_ACTION = "validate_node_config"

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
    "nightly_prefix_caching": (lambda v: isinstance(v, bool), "a boolean"),
    "nightly_ngram": (lambda v: isinstance(v, bool), "a boolean"),
}

# Not vLLM engine fields: the worker would route them out of overrides.
CAPABILITY_ONLY_KEYS = frozenset(
    {"gpu_devices", "base_residency_mb", "min_context_fraction", "nightly_prefix_caching", "nightly_ngram"}
)


def conflicts(capability: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
    """Keys set in both places to different values."""
    return {
        key: (capability[key], overrides[key])
        for key in capability.keys() & overrides.keys()
        if capability[key] != overrides[key]
    }


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-\[\]]*$")
_HINT_START = "(did you mean "
_HINT_END = "?)"


def _split_suggestion(message: str) -> tuple[str, str | None]:
    """The message without a closing "(did you mean x?)", and x; the message itself when it has none."""
    text = message.rstrip()
    start = text.rfind(_HINT_START)
    if start == -1 or not text.endswith(_HINT_END):
        return message, None
    suggestion = text[start + len(_HINT_START) : -len(_HINT_END)]
    if not suggestion or "?" in suggestion:
        return message, None
    return text[:start].rstrip(), suggestion


def error_items(messages: Iterable[str], models: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Messages ("model: key: text") as model, key, message and suggestion.

    ``models`` are the names of the submitted entries. A message starts with one
    of them as its model, even when the name contains ": "; without a match the
    text before the first ": " is not taken for a model.
    """
    names = sorted({name for name in models if name}, key=len, reverse=True)
    items = []
    for message in messages:
        message, suggestion = _split_suggestion(message)
        model = next((name for name in names if message.startswith(f"{name}: ")), None)
        rest = message[len(model) + 2 :] if model is not None else message
        key = None
        parts = rest.split(": ", 1)
        if len(parts) == 2 and model is not None and _KEY.match(parts[0]):
            key, rest = parts
        items.append({"model": model, "key": key, "message": rest, "suggestion": suggestion})
    return items


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


def kv_size_mb(text: Any) -> float | None:
    """A KV cache size like ``"8G"`` in megabytes; ``None`` when it is not one."""
    value = str(text or "").strip().upper()
    if not value or not _KV_SIZE.match(value):
        return None
    unit = value[-1]
    if unit == "G":
        return float(value[:-1]) * 1024.0
    if unit == "M":
        return float(value[:-1])
    if unit == "K":
        return float(value[:-1]) / 1024.0
    return float(value) / (1024.0 * 1024.0)


def selection_of(
    capability: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None
) -> tuple[float | None, int | None, int | None]:
    """The KV cache size (MB), context length and tensor-parallel size chosen for a model.

    Each value may sit under capability or under overrides, and a plan takes
    capability first, so the same order applies here.
    """
    capability, overrides = capability or {}, overrides or {}

    def chosen(key: str) -> Any:
        return capability[key] if key in capability else overrides.get(key)

    def whole(value: Any) -> int | None:
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    return (
        kv_size_mb(chosen("kv_cache_memory_bytes")),
        whole(chosen("max_model_len")),
        whole(chosen("tensor_parallel_size")),
    )


def _measurement_of(profile: dict[str, Any], tp: int | None) -> tuple[Any, Any, list[Any]] | None:
    """The KV range and the context sweep that belong to one tensor-parallel size; None when it was not measured.

    Without a size, or for a calibration that knows one size only (older ones), the profile's own values
    stand for it. A size the calibration cannot show to be measured does not count against the choice when
    nothing is known about the sizes.
    """
    own = (
        profile.get("min_kv_cache_mb"),
        profile.get("max_kv_cache_mb"),
        profile.get("kv_cache_to_max_model_len_pairs") or [],
    )
    if tp is None:
        return own
    sizes = [run for run in profile.get("calibrations_by_tp") or [] if isinstance(run, dict)]
    if sizes:
        run = next((r for r in sizes if r.get("tp") == tp), None)
        if run is None:
            return None
        return run.get("min_kv_cache_mb"), run.get("max_kv_cache_mb"), run.get("kv_cache_to_max_model_len_pairs") or []
    own_tp = profile.get("tensor_parallel_size")
    if isinstance(own_tp, int) and own_tp > 0 and own_tp != tp:
        return None
    return own


def selection_covered(profile: dict[str, Any], kv_mb: float | None, context: int | None, tp: int | None = None) -> bool:
    """Whether a calibration covers the KV cache size, context length and tensor-parallel size chosen for a node.

    Nothing chosen, or nothing known about the measured range, is covered: only a
    value the calibration can show to be out of range counts against it. The range is that
    of the chosen tensor-parallel size; a size that was not measured is not covered.
    """
    measured = _measurement_of(profile, tp)
    if measured is None:
        return False
    low, high, pairs = measured
    if kv_mb is not None and low is not None and high is not None:
        if not (float(low) - 1e-6 <= kv_mb <= float(high) + 1e-6):
            return False
    if context is not None and isinstance(pairs, list) and pairs:
        sweep = [pair for pair in pairs if isinstance(pair, dict) and int(pair.get("max_model_len") or 0) > 0]
        if not sweep:
            return True
        reachable = [
            int(pair["max_model_len"])
            for pair in sweep
            if kv_mb is None or float(pair.get("kv_mb") or 0) <= kv_mb + 1e-6
        ]
        # A KV cache below the smallest measured size reaches no measured context: the sweep cannot vouch for it.
        return bool(reachable) and context <= max(reachable)
    return True
