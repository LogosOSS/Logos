"""Ask a separate Logos worker to select one admissible nightly trial."""

import asyncio
import importlib
import json
import logging
import math
import secrets

from starlette.responses import JSONResponse

from logos.agent_credential import issue_agent_credential
from logos.auth import AuthContext, authenticate_api_key
from logos.benchmarks.guidellm_runner import benchmark_model_lane, lane_is_serving
from logos.benchmarks.reservations import benchmark_reservations
from logos.dbutils.dbmanager import DBManager
from logos.dbutils.types import Deployment

logger = logging.getLogger(__name__)

PROPOSER_MODEL = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
PROPOSAL_TIMEOUT_SECONDS = 10.0
MAX_CANDIDATES = 24
MAX_OBSERVATIONS = 16
DTYPE_CHOICES = {
    "auto",
    "fp8",
    "fp8_e4m3",
    "fp8_e5m2",
    "turboquant_k8v4",
    "turboquant_k3v4_nc",
    "turboquant_4bit_nc",
    "turboquant_3bit_nc",
}


def _parameters(raw: object) -> dict:
    """Select only the known, non-sensitive tuning fields."""
    if not isinstance(raw, dict):
        return {}
    result = {key: raw[key] for key in ("max_num_seqs", "max_num_batched_tokens") if type(raw.get(key)) is int}
    dtype = raw.get("kv_cache_dtype")
    if isinstance(dtype, str) and dtype in DTYPE_CHOICES:
        result["kv_cache_dtype"] = dtype
    spec = raw.get("speculative_config")
    if isinstance(spec, str):
        if not spec:
            result["speculative_config"] = "off"
        elif len(spec) <= 120:
            try:
                decoded = json.loads(spec)
            except ValueError:
                decoded = None
            if (
                isinstance(decoded, dict)
                and decoded.get("method") in ("mtp", "qwen3_5_mtp")
                and type(decoded.get("num_speculative_tokens")) is int
            ):
                result["speculative_config"] = {
                    "method": decoded["method"],
                    "num_speculative_tokens": decoded["num_speculative_tokens"],
                }
    return result


def _observation(raw: dict) -> dict:
    metrics = {
        key: raw[key]
        for key in ("throughput", "p95_latency_ms", "ttft_ms", "tpot_ms")
        if type(raw.get(key)) in (float, int) and math.isfinite(raw[key])
    }
    for key in ("eligible", "baseline"):
        if type(raw.get(key)) is bool:
            metrics[key] = raw[key]
    if raw.get("status") in ("success", "failed", "skipped"):
        metrics["status"] = raw["status"]
    return {"parameters": _parameters(raw.get("parameters")), **metrics}


def _agent_access() -> tuple[AuthContext | None, list[Deployment]]:
    # Use the same managed, revocable, local-only identity as the agent model gateway.
    with DBManager() as db:
        key_id = db.get_agent_api_key_id()
    if key_id is None:
        return None, []
    auth = authenticate_api_key({"Authorization": f"Bearer {issue_agent_credential(key_id)}"})
    with DBManager() as db:
        return auth, db.get_deployments_for_api_key(auth.api_key_id)


def _gpu_ids(snapshot: dict) -> set[str]:
    devices = ((snapshot.get("runtime") or {}).get("devices") or {}).get("devices") or []
    return {
        device["device_id"]
        for device in devices
        if device.get("kind") == "nvidia" and str(device.get("device_id", "")).startswith("GPU-")
    }


def _ready_workers(registry, excluded_worker_id: int) -> set[int]:
    providers = registry.active_provider_ids()
    blocked = {excluded_worker_id} | {
        provider_id for provider_id in providers if benchmark_reservations.provider_owner(provider_id) is not None
    }
    snapshots = {provider_id: (registry.peek_runtime_snapshot(provider_id) or {}) for provider_id in providers}
    # A worker may share a name or accelerator with another registered provider.
    blocked_worker_ids = {snapshots.get(provider_id, {}).get("worker_id") for provider_id in blocked} - {None}
    blocked_gpu_ids = set().union(*(_gpu_ids(snapshots.get(provider_id, {})) for provider_id in blocked))
    ready = set()
    for provider_id, snapshot in snapshots.items():
        health = (snapshot.get("runtime") or {}).get("node_health") or {}
        if (
            provider_id in blocked
            or snapshot.get("worker_id") in blocked_worker_ids
            or blocked_gpu_ids.intersection(_gpu_ids(snapshot))
            or not registry.is_provider_online(provider_id)
            or not registry.has_received_first_status(provider_id)
            or registry.is_calibrating(provider_id)
            or registry.is_cordoned(provider_id)
            or health.get("healthy", True) is False
        ):
            continue
        lane = benchmark_model_lane(snapshot, PROPOSER_MODEL)
        if lane_is_serving(lane) and not lane.get("transition"):
            ready.add(provider_id)
    return ready


def _request(candidates: list[dict], observations: list[dict], limits: dict, seconds: float) -> dict:
    context = {
        "candidates": [{"candidate_id": index, "parameters": _parameters(row)} for index, row in enumerate(candidates)],
        "observations": [_observation(row) for row in observations],
        "baseline_parameters": _parameters(limits.get("current")),
        "gpu_limits": {key: limits[key] for key in ("gpu_count", "gpu_memory_bytes") if type(limits.get(key)) is int},
        "remaining_seconds": int(seconds),
    }
    return {
        "model": PROPOSER_MODEL,
        "stream": False,
        "temperature": 0,
        "max_tokens": 32,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Select one candidate ID to test next in a vLLM serving benchmark. "
                    "Candidate and observation parameters are overrides of the baseline parameters. "
                    "Use the measured configurations and remaining time to find higher output throughput "
                    "without increasing p95 request latency more than 10% against the baseline. "
                    "Balance improving the best eligible measurement and exploring untested settings. "
                    "Return only the JSON object containing the candidate ID."
                ),
            },
            {"role": "user", "content": json.dumps(context)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "benchmark_proposal",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"candidate_id": {"type": "integer", "enum": list(range(len(candidates)))}},
                    "required": ["candidate_id"],
                    "additionalProperties": False,
                },
            },
        },
    }


def _choice(response: JSONResponse, candidate_count: int) -> int | None:
    if not isinstance(response, JSONResponse) or response.status_code != 200:
        return None
    try:
        content = json.loads(response.body)["choices"][0]["message"]["content"]
        proposal = json.loads(content)
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    if not isinstance(proposal, dict) or set(proposal) != {"candidate_id"}:
        return None
    index = proposal["candidate_id"]
    return index if type(index) is int and 0 <= index < candidate_count else None


async def propose(
    *,
    excluded_worker_id: int,
    candidates: list[dict],
    observations: list[dict],
    remaining_seconds: float,
    limits: dict | None = None,
) -> int | None:
    """Return an index in the supplied options or defer to the scheduler's rule."""
    gateway = importlib.import_module("logos.main")

    if len(candidates) < 2 or remaining_seconds < 1 or gateway._logosnode_registry is None:
        return None

    options = candidates[:MAX_CANDIDATES]
    history = (
        observations
        if len(observations) <= MAX_OBSERVATIONS
        else observations[:1] + observations[-(MAX_OBSERVATIONS - 1) :]
    )
    deadline = min(remaining_seconds, PROPOSAL_TIMEOUT_SECONDS)
    try:
        async with asyncio.timeout(deadline):
            registry = gateway._logosnode_registry
            if not _ready_workers(registry, excluded_worker_id):
                return None

            auth, deployments = await asyncio.to_thread(_agent_access)
            if auth is None:
                return None
            ready = _ready_workers(registry, excluded_worker_id)
            local = [
                row
                for row in deployments
                if row["provider_id"] in ready and row["type"] == "logosnode" and row["model_name"] == PROPOSER_MODEL
            ]
            body = _request(options, history, limits or {}, remaining_seconds)
            eligible = await gateway._filter_logosnode_deployments(local, payload=body)
            if not eligible:
                return None
            selected = eligible[0]
            provider_id = selected["provider_id"]
            if provider_id not in _ready_workers(registry, excluded_worker_id):
                return None

            auth.resolved_proxy_model = selected["model_id"], PROPOSER_MODEL
            response = await gateway.route_and_execute(
                deployments=[selected],
                body=body,
                headers={},
                auth=auth,
                path="v1/chat/completions",
                log_id=None,
                request_id=secrets.token_urlsafe(16),
                required_provider_id=provider_id,
            )
            try:
                return _choice(response, len(options))
            finally:
                if not isinstance(response, JSONResponse):
                    await gateway._discard_response(response)
    except Exception as exc:
        logger.debug("Nightly proposal fell back to rules (%s)", type(exc).__name__)
        return None
