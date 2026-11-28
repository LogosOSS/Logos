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


def _valid_logos_base(value: str | None) -> bool:
    # Reject malformed network coordinates rather than interpreting them as a
    # different origin to the one the provider's relay will dial.
    if not value or value != value.strip() or any(ord(ch) < 33 or ch in "\\?#" for ch in value):
        return False
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
            or url.path not in ("", "/", "/v1", "/v1/")
        ):
            return False
        host = url.hostname
        if "%" in host or (":" in host and not re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?", url.netloc)):
            return False
        if ":" in host or host.replace(".", "").isdigit():
            ip_address(host)
        else:
            labels = host.removesuffix(".").split(".")
            if (len(labels) > 1 and not labels[-1][:1].isalpha()) or not all(
                _DNS_LABEL.fullmatch(label) for label in labels
            ):
                return False
        return True
    except ValueError:
        return False


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


def remote_transport_configured() -> bool:
    try:
        remote_network()
    except ValueError:
        return False
    return True


async def _cloud_catalog() -> list[dict[str, Any]]:
    # The provider registry is managed separately by Logos admins. A choice
    # never accepts a URL from an Agent UI client or workspace.
    if not remote_transport_configured():
        return []
    return [row for row in await db.agent_cloud_deployments() if _valid_logos_base(row["base_url"])]


def _matches_pin(row: dict[str, Any], provider_id: int, model_id: int, model: str | None, base_url: str) -> bool:
    return (
        row["provider_id"] == provider_id
        and row["model_id"] == model_id
        and row["model"] == model
        and row["base_url"] == base_url
    )


def _cloud_selection_valid(selected: dict[str, Any], catalog: list[dict[str, Any]]) -> bool:
    if selected.get("mode") != "cloud":
        return True
    provider_id = selected.get("provider_id")
    model_id = selected.get("model_id")
    base_url = selected.get("provider_base_url")
    return (
        provider_id is not None
        and model_id is not None
        and base_url is not None
        and any(_matches_pin(row, provider_id, model_id, selected.get("model"), base_url) for row in catalog)
    )


async def automation_selection() -> dict[str, Any] | None:
    """Return the authorized default to pin, or None when no automatic work should be queued."""
    selected = await db.agent_inference_selection()
    mode = selected.get("mode", "local")
    model = selected.get("model")
    if mode == "local":
        policy = model_policy.current()
        return selected if policy.ok and (model is None or policy.allows(model)) else None
    if mode == "cloud" and _cloud_selection_valid(selected, await _cloud_catalog()):
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
    base_url = session.get("inference_base_url")
    if provider_id is None or model_id is None or base_url is None:
        return False
    if not await db.agent_cloud_allowed_for_repository(
        provider_id,
        session.get("team_repository_id"),
        session.get("repo_slug"),
        workflow_analysis=session.get("trigger_kind") == "analysis",
        session_id=session.get("id"),
    ):
        return False
    return any(
        _matches_pin(row, provider_id, model_id, session.get("model"), base_url) for row in await _cloud_catalog()
    )


async def issue_remote_grant(session_id: int) -> str | None:
    """Store only a digest for the still-starting session."""
    grant = secrets.token_urlsafe(32)
    digest = hashlib.sha256(grant.encode("ascii")).hexdigest()
    return grant if await db.set_agent_inference_grant(session_id, digest) else None


async def view() -> dict[str, Any]:
    selected = await db.agent_inference_selection()
    policy = await model_policy.refresh()
    catalog = await _cloud_catalog()
    return {
        "mode": selected.get("mode", "local"),
        "model": selected.get("model"),
        "provider_id": selected.get("provider_id"),
        "model_id": selected.get("model_id"),
        "local_models": list(policy.offered),
        "cloud_options": [
            {key: row[key] for key in ("provider_id", "provider_name", "model_id", "model")} for row in catalog
        ],
        "cloud_selection_valid": _cloud_selection_valid(selected, catalog),
    }


async def save(
    mode: str, model: str | None, provider_id: int | None, model_id: int | None, author: str
) -> dict[str, Any]:
    base_url = None
    if mode == "cloud":
        options = await _cloud_catalog()
        allowed = next(
            (row for row in options if row["provider_id"] == provider_id and row["model_id"] == model_id), None
        )
        if allowed is None or (model is not None and model != allowed["model"]):
            raise ValueError("Select an approved, authenticated cloud model deployment")
        model = allowed["model"]
        base_url = allowed["base_url"]
    else:
        if provider_id is not None or model_id is not None:
            raise ValueError("Remote deployment IDs do not apply to a local model")
        if model is not None:
            policy = await model_policy.refresh()
            if not policy.allows(model):
                raise ValueError(policy.refusal(model))
            model = policy.canonical(model)
    await db.save_agent_inference_selection(mode, model, provider_id, model_id, author, provider_base_url=base_url)
    return await view()
