"""Per-node model configuration that the worker reads from Logos.

An entry is {"model_name", "capability", "overrides"}: ``capability`` is what
config.yml writes inline under ``logos.capabilities_models``, ``overrides``
what it writes under ``engines.vllm.model_overrides``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from logos_worker_node.config import _wire_kv_budget
from logos_worker_node.models import AppConfig

SYNC_NODE_CONFIG_ACTION = "sync_node_config"
VALIDATE_NODE_CONFIG_ACTION = "validate_node_config"


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def normalize_node_config(body: Any) -> dict[str, Any]:
    """Logos' answer as {revision, yaml_imported, entries}; raises if unusable."""
    if not isinstance(body, dict) or not isinstance(body.get("entries"), list):
        raise RuntimeError("Logos node-config response has no entries list")
    entries = []
    for item in body["entries"]:
        name = str(_mapping(item).get("model_name") or "").strip()
        if not name:
            raise RuntimeError("Logos node-config response has an entry without a model_name")
        entries.append(
            {
                "model_name": name,
                "capability": dict(_mapping(item.get("capability"))),
                "overrides": dict(_mapping(item.get("overrides"))),
            }
        )
    return {
        "revision": int(body.get("revision") or 0),
        "yaml_imported": bool(body.get("yaml_imported")),
        "entries": entries,
    }


def entries_from_yaml(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """The models of a config.yml, exactly as the file states them.

    Calibration keys hash these values, so nothing is added or normalized.
    """
    logos = _mapping(raw.get("logos"))
    explicit = _mapping(logos.get("capabilities_overrides"))
    model_overrides = _mapping(_mapping(_mapping(raw.get("engines")).get("vllm")).get("model_overrides"))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in logos.get("capabilities_models") or []:
        if isinstance(item, str):
            name, capability = item, {}
        elif isinstance(item, dict) and item.get("model"):
            name = str(item["model"])
            capability = {key: value for key, value in item.items() if key != "model"}
        else:
            continue
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        for key, value in _mapping(explicit.get(name)).items():
            capability.setdefault(key, value)
        entries.append(
            {"model_name": name, "capability": capability, "overrides": dict(_mapping(model_overrides.get(name)))}
        )
    return entries


def entries_from_yaml_file(config_path: Path) -> list[dict[str, Any]]:
    with config_path.open() as f:
        return entries_from_yaml(yaml.safe_load(f) or {})


def plans_from_entries(entries: list[dict[str, Any]], global_args: list[str]) -> list[dict[str, Any]]:
    """Calibration plans: capability first, then overrides for what is unset."""
    plans: list[dict[str, Any]] = []
    for entry in entries:
        plan: dict[str, Any] = {"model": entry["model_name"], **entry["capability"]}
        # All overrides count: the plan must describe the lane that is served.
        for key, value in entry["overrides"].items():
            if key != "model" and not key.startswith("_"):
                plan.setdefault(key, value)
        # Worker-wide flags first, so a model's own extra_args win.
        if global_args:
            plan["extra_args"] = [*global_args, *(plan.get("extra_args") or [])]
        plans.append(plan)
    return plans


def plans_from_app_config(cfg: AppConfig) -> list[dict[str, Any]]:
    """Plans of the models this worker was given; empty before it has any."""
    entries = cfg.logos.node_entries if cfg.logos else None
    global_args = [str(arg) for arg in (cfg.engines.vllm.global_extra_args if cfg.engines else None) or []]
    return plans_from_entries(entries or [], global_args)


def apply_node_config(cfg: AppConfig, entries: list[dict[str, Any]], revision: int) -> None:
    """Make ``entries`` the worker's models, replacing those from config.yml.

    The entries stay untouched in ``node_entries``: wiring the KV budget
    below adds keys that must not reach the calibration plans.
    """
    entries = copy.deepcopy(entries)
    cfg.logos.node_entries = copy.deepcopy(entries)
    cfg.logos.node_config_revision = revision
    names = [entry["model_name"] for entry in entries]
    cfg.logos.capabilities_models = list(names)
    cfg.logos.configured_models = list(names)
    cfg.logos.capabilities_overrides = {e["model_name"]: e["capability"] for e in entries if e["capability"]}
    model_overrides = cfg.engines.vllm.model_overrides
    for entry in entries:
        if entry["overrides"]:
            model_overrides[entry["model_name"]] = entry["overrides"]
        else:
            model_overrides.pop(entry["model_name"], None)
    _wire_kv_budget(cfg)
