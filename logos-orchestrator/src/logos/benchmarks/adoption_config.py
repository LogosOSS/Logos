"""Writes the winner of a nightly tuning into the node's central model configuration.

The lane already runs with the winner's values; without this a later sync of the
configuration would put the old ones back.
"""

import asyncio
import copy
import logging
from typing import Any, Dict, Mapping

from logos.benchmarks.configuration import ServingOverrides
from logos.benchmarks.guidellm_runner import apply_serving_overrides
from logos.dbutils.dbmanager import DBManager
from logos.dbutils.dbrequest import NodeConfigEntry

logger = logging.getLogger(__name__)

# What the configuration keeps under capability; everything else is an override.
CAPABILITY_KEYS = ("tensor_parallel_size", "kv_cache_memory_bytes")
TUNING_SOURCE = "tuning"


def tuned_entry(entry: Mapping[str, Any], parameters: Mapping[str, Any]) -> Dict[str, Any]:
    """The model's configuration with the values a tuning winner runs with.

    ``max_num_batched_tokens`` has no field of its own; it becomes a flag in
    ``extra_args``, replacing one that was set before.
    """
    capability = dict(entry.get("capability") or {})
    overrides = dict(entry.get("overrides") or {})
    wanted = ServingOverrides(**dict(parameters))
    current = {**overrides, **{key: capability[key] for key in CAPABILITY_KEYS if key in capability}}
    merged = apply_serving_overrides(current, wanted)
    for key in CAPABILITY_KEYS:
        if key in merged and (key in capability or getattr(wanted, key) is not None):
            capability[key] = merged.pop(key)
    return {"model_name": entry["model_name"], "capability": capability, "overrides": merged}


async def record_adoption(provider_id: int, model: str, parameters: Mapping[str, Any]) -> str | None:
    """Store the adopted values; returns why it could not, or None when it did.

    A failure leaves the adoption in place: the lane keeps the values it runs
    with, and the next sync of the configuration is the only thing at stake.
    """
    from logos.routers.logosnode import _load_node_config, save_node_config  # noqa: PLC0415

    reason = "the configuration changed while it was written"
    for _ in range(2):
        config = await asyncio.to_thread(_load_node_config, provider_id)
        if config is None:
            return "the node is unknown"
        entries = copy.deepcopy(config["entries"])
        if not any(entry["model_name"] == model for entry in entries):
            return f"{model} has no configuration to update"
        updated = [tuned_entry(entry, parameters) if entry["model_name"] == model else entry for entry in entries]
        with DBManager() as db:
            sources = db.get_node_config_sources(provider_id)
        result = await save_node_config(
            provider_id,
            [NodeConfigEntry(**entry) for entry in updated],
            int(config["revision"]),
            None,
            source=TUNING_SOURCE,
            source_by_model={**sources, model: TUNING_SOURCE},
        )
        if isinstance(result, dict):
            return None
        status = getattr(result, "status_code", None)
        reason = _reason_of(result)
        if status != 409:
            break
    return reason


def _reason_of(response: Any) -> str:
    import json  # noqa: PLC0415

    try:
        return str(json.loads(response.body).get("error") or "refused")
    except (TypeError, ValueError, AttributeError):
        return "refused"
