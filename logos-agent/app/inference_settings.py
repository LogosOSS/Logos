"""Operator model choice and the eligibility rules for pinned cloud sessions."""

from __future__ import annotations

import hashlib
import re
import secrets
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from . import db, model_policy
from .config import settings

REMOTE_GATEWAY = "http://logos-agent-remote-gateway"
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _https_origin(value: str) -> tuple[str, int] | None:
    # Reject malformed network coordinates rather than interpreting them as a
    # different origin to the one the provider's relay will dial.
    if not value or value != value.strip() or any(ord(ch) < 33 or ch in "\\?#" for ch in value):
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
        host = url.hostname
        if ":" in host or host.replace(".", "").isdigit():
            ip_address(host)
        elif not all(_DNS_LABEL.fullmatch(label) for label in host.rstrip(".").split(".")):
            return None
        return host, url.port or 443
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


def _approved_origins() -> set[tuple[str, int]]:
    origins = [_https_origin(value) for value in settings.remote_allowed_origins]
    if not origins or None in origins:
        return set()
    try:
        remote_network()
    except ValueError:
        return set()
    return {origin for origin in origins if origin is not None}


def remote_transport_configured() -> bool:
    return bool(_approved_origins())


async def cloud_options() -> list[dict[str, Any]]:
    allowed = _approved_origins()
    if not allowed:
        return []
    rows = await db.agent_cloud_deployments()
    return [
        {key: row[key] for key in ("provider_id", "provider_name", "model_id", "model")}
        for row in rows
        if _https_origin(row["base_url"]) in allowed
    ]


async def _approved_deployment(provider_id: int, model_id: int, model: str | None) -> bool:
    return any(
        row["provider_id"] == provider_id and row["model_id"] == model_id and row["model"] == model
        for row in await cloud_options()
    )


async def automation_selection() -> dict[str, Any] | None:
    """Return the authorized default to pin, or None when no automatic work should be queued."""
    selected = await db.agent_inference_selection()
    mode = selected.get("mode", "local")
    model = selected.get("model")
    if mode == "local":
        policy = model_policy.current()
        return selected if policy.ok and (model is None or policy.allows(model)) else None
    if mode == "cloud":
        provider_id = selected.get("provider_id")
        model_id = selected.get("model_id")
        if (
            provider_id is not None
            and model_id is not None
            and await _approved_deployment(provider_id, model_id, model)
        ):
            return selected
    return None


async def permits_automation_repository(
    selected: dict[str, Any],
    team_repository_id: int | None,
    repo_slug: str | None,
    *,
    workflow_analysis: bool = False,
) -> bool:
    """Check the current team policy before creating a new queue entry."""
    if selected.get("mode") != "cloud":
        return True
    return await db.agent_cloud_allowed_for_repository(
        selected["provider_id"], team_repository_id, repo_slug, queue_automatic_analysis=workflow_analysis
    )


async def permits_pin(session: dict[str, Any]) -> bool:
    """Recheck the pin's deployment and its live repository privacy policy."""
    provider_id = session.get("inference_provider_id")
    model_id = session.get("inference_model_id")
    if provider_id is None or model_id is None:
        return False
    if not await db.agent_cloud_allowed_for_repository(
        provider_id,
        session.get("team_repository_id"),
        session.get("repo_slug"),
        workflow_analysis=session.get("trigger_kind") == "analysis",
        session_id=session.get("id"),
    ):
        return False
    return await _approved_deployment(provider_id, model_id, session.get("model"))


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
