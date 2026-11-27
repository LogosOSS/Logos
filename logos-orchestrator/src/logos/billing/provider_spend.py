"""Refresh the monthly cloud spend gate without I/O on the inference path."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from logos.dbutils.dbmanager import DBManager

logger = logging.getLogger(__name__)
_REFRESH_INTERVAL_SECONDS = 1
_MAX_VALIDITY_MS = 5_000
_POLICY_ONLY_VALIDITY_SECONDS = 4
_STARTUP_POLICY_TIMEOUT_SECONDS = 0.5


@dataclass(frozen=True)
class SpendSnapshot:
    blocked_ids: frozenset[int]
    expires_at: float
    unknown_ids: frozenset[int] | None = None


_snapshot: SpendSnapshot | None = None


def _valid_provider_id(value: object) -> bool:
    return type(value) is int and 0 < value < 2**31


def _endpoint() -> tuple[str, str]:
    base = os.getenv("LOGOS_WEBSERVICE_URL", "").rstrip("/")
    secret = os.getenv("LOGOS_INTERNAL_SECRET", "")
    target = urlsplit(base)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username is not None
        or target.query
        or target.fragment
        or not secret.strip()
    ):
        raise ValueError("Provider spend gate is not configured")
    return f"{base}/internal/provider-spend/status", secret


async def _refresh(client: httpx.AsyncClient) -> None:
    global _snapshot
    url, secret = _endpoint()
    started = monotonic()
    response = await client.get(url, headers={"Authorization": f"Bearer {secret}"})
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid provider spend status")
    blocked = body.get("blocked_provider_ids")
    validity = body.get("valid_for_ms")
    if (
        not isinstance(blocked, list)
        or not all(_valid_provider_id(value) for value in blocked)
        or type(validity) is not int
        or not 0 <= validity <= _MAX_VALIDITY_MS
    ):
        raise ValueError("Invalid provider spend status fields")

    # Network time consumes the lease; a slow response cannot renew stale data.
    expires_at = started + validity / 1_000
    if monotonic() >= expires_at:
        raise ValueError("Provider spend status already expired")
    _snapshot = SpendSnapshot(frozenset(blocked), expires_at)


def _read_active_policy_ids() -> frozenset[int]:
    with DBManager() as db:
        return db.get_active_provider_spend_policy_ids()


async def _refresh_policies_if_needed() -> None:
    global _snapshot
    now = monotonic()
    if _snapshot is not None and _snapshot.unknown_ids is None and now < _snapshot.expires_at:
        return
    # Without a live spend snapshot, verified uncapped providers can still
    # operate. Active caps remain unknown and fail closed, never queried by a request.
    active_ids = await asyncio.to_thread(_read_active_policy_ids)
    _snapshot = SpendSnapshot(frozenset(), now + _POLICY_ONLY_VALIDITY_SECONDS, unknown_ids=active_ids)


async def _try_refresh_policies() -> None:
    try:
        await _refresh_policies_if_needed()
    except SQLAlchemyError:
        # Without verified policy information, do not admit any cloud
        # work from a stale or absent snapshot.
        pass


async def _refresh_loop() -> None:
    warned = False
    async with httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False) as client:
        while True:
            try:
                await _refresh(client)
                warned = False
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                # Keep a valid former lease only until its original deadline.
                if not warned:
                    logger.warning("Could not refresh provider monthly spend status: %s", type(exc).__name__)
                    warned = True
                await _try_refresh_policies()
            await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


@asynccontextmanager
async def refreshing():
    """Refresh spend status for this app process and clear it at shutdown."""
    global _snapshot
    _snapshot = None
    # A fresh process can serve confirmed uncapped providers even if the
    # webservice has not come up yet. Do not delay local inference on a slow DB.
    with suppress(TimeoutError):
        await asyncio.wait_for(_try_refresh_policies(), timeout=_STARTUP_POLICY_TIMEOUT_SECONDS)
    task = asyncio.create_task(_refresh_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        _snapshot = None


async def check(provider_id: int) -> None:
    """Use only the in-process lease; never wait or perform I/O in a request."""
    snapshot = _snapshot
    if (
        not _valid_provider_id(provider_id)
        or snapshot is None
        or monotonic() >= snapshot.expires_at
        or (snapshot.unknown_ids is not None and provider_id in snapshot.unknown_ids)
    ):
        raise HTTPException(503, "Provider monthly spend limit is unavailable")
    if provider_id in snapshot.blocked_ids:
        raise HTTPException(402, "Provider monthly spend limit reached")
