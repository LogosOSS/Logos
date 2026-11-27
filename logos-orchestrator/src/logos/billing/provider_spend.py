"""The orchestrator asks the Java ledger before its existing provider I/O.

Only the shape of a potential text request travels to the ledger: no prompt
text, user identifier or upstream authorization header is necessary to bound it.
"""

import logging
import os
import uuid
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

_FIELDS = frozenset("""model messages max_tokens max_completion_tokens max_output_tokens
    stream stream_options temperature top_p stop n timeout_s service_tier
    frequency_penalty presence_penalty seed user reasoning_effort system
    stop_sequences top_k input instructions role content name type text""".split())
_META = {"type", "model"}
_POSITIVE_INTEGERS = {"max_tokens", "max_completion_tokens", "max_output_tokens", "n"}


def _shape(value, field="", depth=0):
    if depth > 8:
        return {"unsupported": None}
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key not in _FIELDS:
                result["unsupported"] = None
            else:
                result[key] = _shape(item, key, depth + 1)
        return result
    if isinstance(value, list):
        return [_shape(item, field, depth + 1) for item in value] if field in {"messages", "content"} else []
    if isinstance(value, str):
        if field == "model":
            return value if len(value) <= 200 else ""
        return value if field in _META and value == "text" else ""
    if field in _POSITIVE_INTEGERS:
        return value if type(value) is int and 0 < value <= 2**31 - 1 else None
    return None


async def _call(operation: str, body: dict) -> httpx.Response:
    base = os.getenv("LOGOS_WEBSERVICE_URL", "").rstrip("/")
    secret = os.getenv("LOGOS_INTERNAL_SECRET", "")
    target = urlsplit(base)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username is not None
        or target.query
        or target.fragment
        or not secret
    ):
        raise RuntimeError("Provider spend ledger is not configured")
    async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
        return await client.post(
            f"{base}/internal/provider-spend/{operation}", headers={"Authorization": f"Bearer {secret}"}, json=body
        )


async def reserve(
    provider_id: int,
    model_id: int | None = None,
    *,
    url: str = "",
    payload: dict | None = None,
) -> uuid.UUID | None:
    """Reserve against the final cloud provider, or fail before upstream I/O."""
    try:
        response = await _call(
            "reserve",
            {"providerId": provider_id, "modelId": model_id, "upstreamUrl": url or None, "shape": _shape(payload)},
        )
        if response.status_code == 402:
            raise HTTPException(402, "Provider monthly spend limit reached")
        if response.status_code == 422:
            raise HTTPException(422, "Request cost cannot be bounded under this provider's monthly spend limit")
        response.raise_for_status()
        attempt = response.json()["attemptId"]
        return None if attempt is None else uuid.UUID(attempt)
    except HTTPException:
        raise
    except (httpx.HTTPError, KeyError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
        logger.warning("Could not check provider spend cap for provider %s: %s", provider_id, type(exc).__name__)
        raise HTTPException(503, "Provider monthly spend limit is unavailable") from exc


async def settle(attempt_id: uuid.UUID | None, usage: dict, service_tier: str | None) -> None:
    """Record the price only when the response has complete, nonestimated usage."""
    if attempt_id is None or "usage_estimated" in usage:
        return
    if not all(type(usage.get(name)) is int and usage[name] >= 0 for name in ("prompt_tokens", "completion_tokens")):
        return
    await _settle(
        attempt_id,
        {"attemptId": str(attempt_id), "usage": usage, "tier": service_tier, "undispatched": False},
    )


async def release_undispatched(attempt_id: uuid.UUID | None) -> None:
    """Release only when the executor can prove it never started the send."""
    if attempt_id is not None:
        await _settle(attempt_id, {"attemptId": str(attempt_id), "undispatched": True})


async def _settle(attempt_id: uuid.UUID, body: dict) -> None:
    try:
        (await _call("settle", body)).raise_for_status()
    except (httpx.HTTPError, ValueError, RuntimeError, TypeError) as exc:
        logger.warning(
            "Provider spend attempt %s could not settle (%s); keeping reservation", attempt_id, type(exc).__name__
        )
