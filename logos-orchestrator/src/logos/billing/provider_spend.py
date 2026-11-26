"""Shared hard-cap admission for cloud attempts.

The SQL function is also used by the webservice. A new ID represents each
upstream attempt, including retries. A transport failure is not proof of no
charge; only complete reported usage can settle a dispatched attempt.
"""

import json
import logging
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import text

from logos.dbutils.dbmanager import DBManager

logger = logging.getLogger(__name__)


def _kind(url: str) -> str | None:
    """Return a supported provider protocol, not an arbitrary compatible API."""
    try:
        target = urlsplit(url)
        host = target.hostname or ""
        if target.scheme != "https" or target.username or target.password or target.port not in (None, 443):
            return None
    except ValueError:
        return None
    if target.query or target.fragment:
        return None
    if host == "api.openai.com":
        return {"/v1/chat/completions": "chat", "/v1/responses": "responses"}.get(target.path)
    if host == "api.anthropic.com" and target.path == "/v1/messages":
        return "messages"
    return None


def reserve(
    provider_id: int,
    model_id: int | None = None,
    *,
    url: str = "",
    payload: dict | None = None,
) -> uuid.UUID | None:
    """Reserve against the final cloud provider, or fail before upstream I/O.

    Supplying neither a URL nor a payload fences unsupported features for an
    active cap while leaving those features unchanged for uncapped providers.
    """
    attempt_id = uuid.uuid4()
    kind = _kind(url)
    try:
        with DBManager() as db:
            row = db.session.execute(
                text("""
                    SELECT logos_reserve_provider_spend(
                        :attempt, :provider, :model, :kind, CAST(:payload AS jsonb)
                    )
                    """),
                {
                    "attempt": attempt_id,
                    "provider": provider_id,
                    "model": model_id,
                    "kind": kind,
                    "payload": json.dumps(payload) if kind and payload is not None else None,
                },
            ).scalar_one()
            db.session.commit()
    except Exception as exc:
        logger.warning("Could not check provider spend cap for provider %s: %s", provider_id, type(exc).__name__)
        raise HTTPException(503, "Provider monthly spend limit is unavailable") from exc
    if row == "uncapped":
        return None
    if row == "reserved":
        return attempt_id
    if row == "exhausted":
        raise HTTPException(402, "Provider monthly spend limit reached")
    if row == "unbounded":
        raise HTTPException(422, "Request cost cannot be bounded under this provider's monthly spend limit")
    raise HTTPException(503, "Provider monthly spend reservation could not be confirmed")


def settle(attempt_id: uuid.UUID | None, usage: dict, service_tier: str | None) -> None:
    """Record the price only when the response has complete, nonestimated usage."""
    if attempt_id is None or "usage_estimated" in usage:
        return
    if not all(type(usage.get(name)) is int for name in ("prompt_tokens", "completion_tokens")):
        return
    try:
        with DBManager() as db:
            db.session.execute(
                text("SELECT logos_settle_provider_spend(:attempt, CAST(:usage AS jsonb), :tier)"),
                {"attempt": attempt_id, "usage": json.dumps(usage), "tier": service_tier},
            )
            db.session.commit()
    except Exception:
        logger.exception("Provider spending attempt %s could not settle; keeping reservation", attempt_id)


def release_undispatched(attempt_id: uuid.UUID | None) -> None:
    """Release only when the executor can prove it never started the send."""
    if attempt_id is None:
        return
    try:
        with DBManager() as db:
            db.session.execute(
                text("SELECT logos_settle_provider_spend(:attempt, NULL, NULL, true)"),
                {"attempt": attempt_id},
            )
            db.session.commit()
    except Exception:
        logger.exception("Undispatched provider spending attempt %s could not release", attempt_id)
