"""Operator model choice and the eligibility rules for pinned cloud sessions."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from urllib.parse import urlsplit

from . import db, model_policy
from .config import settings

REMOTE_GATEWAY = "http://logos-agent-remote-gateway"


def _https_origin(value: str) -> tuple[str, int] | None:
    # Reject malformed network coordinates rather than interpreting them as a
    # different origin to the one the provider's relay will dial.
    if not value or value != value.strip() or any(ord(ch) < 33 or ch == "\\" for ch in value):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.port == 0
            or url.netloc.endswith(":")
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            return None
        return url.hostname, url.port or 443
    except ValueError:
        return None


def remote_network() -> str:
    network = settings.remote_inference_network
    if network in {"", "bridge", "host", "none", settings.session_network, settings.session_egress_network}:
        raise ValueError("Remote agent inference requires its own isolated session network")
    return network


def is_remote(session: dict[str, Any]) -> bool:
    # Treat either nonnull ID as a remote pin; a malformed half pin cannot
    # downgrade this session into the old local route.
    return session.get("inference_provider_id") is not None or session.get("inference_model_id") is not None


def session_network(session: dict[str, Any]) -> str:
    return remote_network() if is_remote(session) else settings.session_network


async def cloud_options() -> list[dict[str, Any]]:
    allowed = {_https_origin(value) for value in settings.remote_allowed_origins} - {None}
    if not allowed:
        return []
    try:
        remote_network()
    except ValueError:
        return []
    rows = await db.agent_cloud_deployments()
    return [
        {key: row[key] for key in ("provider_id", "provider_name", "model_id", "model")}
        for row in rows
        if _https_origin(row["base_url"]) in allowed
    ]


async def permits_pin(session: dict[str, Any]) -> bool:
    """Recheck the configured deployment and its model identity before launch."""
    return any(
        row["provider_id"] == session.get("inference_provider_id")
        and row["model_id"] == session.get("inference_model_id")
        and row["model"] == session.get("model")
        for row in await cloud_options()
    )


async def issue_remote_grant(session_id: int) -> str | None:
    """Store only a digest for the still-starting session."""
    grant = secrets.token_urlsafe(32)
    digest = hashlib.sha256(grant.encode("ascii")).hexdigest()
    return grant if await db.set_agent_inference_grant(session_id, digest) else None


async def view() -> dict[str, Any]:
    selected = await db.agent_inference_selection()
    policy = await model_policy.refresh()
    return {
        "mode": selected.get("mode", "local"),
        "model": selected.get("model"),
        "provider_id": selected.get("provider_id"),
        "model_id": selected.get("model_id"),
        "local_models": list(policy.offered),
        "cloud_options": await cloud_options(),
    }


async def save(
    mode: str, model: str | None, provider_id: int | None, model_id: int | None, author: str
) -> dict[str, Any]:
    if mode == "cloud":
        options = await cloud_options()
        allowed = next(
            (row for row in options if row["provider_id"] == provider_id and row["model_id"] == model_id), None
        )
        if allowed is None or (model is not None and model != allowed["model"]):
            raise ValueError("Select an approved, authenticated cloud model deployment")
        model = allowed["model"]
    else:
        if provider_id is not None or model_id is not None:
            raise ValueError("Remote deployment IDs do not apply to a local model")
        if model is not None:
            policy = await model_policy.refresh()
            if not policy.allows(model):
                raise ValueError(policy.refusal(model))
            model = policy.canonical(model)
    await db.save_agent_inference_selection(mode, model, provider_id, model_id, author)
    return await view()
