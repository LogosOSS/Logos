"""Verify Keycloak identity and authorize agent operations with the active Logos user role."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt import PyJWKClient
from sqlalchemy.exc import SQLAlchemyError

from .config import settings
from .db import active_user_role

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]

    def has_role(self, role: str) -> bool:
        return role in self.roles


_jwk_client: PyJWKClient | None = None
_jwk_client_created_at: float = 0.0
# Keycloak rotates signing keys; refetch the key set periodically so a rotation
# does not take the service down until a restart.
_JWKS_TTL_S = 3600.0


def _client() -> PyJWKClient:
    global _jwk_client, _jwk_client_created_at
    now = time.monotonic()
    if _jwk_client is None or now - _jwk_client_created_at > _JWKS_TTL_S:
        if not settings.keycloak_jwks_uri:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="KEYCLOAK_JWKS_URI is not configured",
            )
        _jwk_client = PyJWKClient(settings.keycloak_jwks_uri, cache_keys=True)
        _jwk_client_created_at = now
    return _jwk_client


def _audience_ok(claims: dict[str, Any]) -> bool:
    """Whether the token was issued for the configured Logos client.

    Mirrors the webservice's resource-server check (SecurityConfig's
    AudienceValidator): Keycloak carries the authorized party in ``aud`` for
    some flows and in ``azp`` for the standard browser flow, so the client ID
    is accepted in either place. A token that presents neither is not one for
    this client, and no role claim can make it one.
    """
    expected = settings.keycloak_audience
    if not expected:
        return True
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience = [audience]
    in_aud = isinstance(audience, list) and any(entry == expected for entry in audience if isinstance(entry, str))
    return in_aud or claims.get("azp") == expected


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return header[7:].strip()


async def current_principal(request: Request) -> Principal:
    if settings.auth_disabled:
        if not settings.dev_mode:
            # Refusing here rather than at startup keeps the failure visible in
            # the response instead of hidden in a crash loop.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="LOGOS_AGENT_AUTH_DISABLED requires LOGOS_AGENT_DEV_MODE",
            )
        return Principal(subject="dev", username="dev", roles=frozenset({settings.required_role}))

    token = _bearer(request)
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
            issuer=settings.keycloak_issuer_uri or None,
            options={"verify_iss": bool(settings.keycloak_issuer_uri), "verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}")
    except (httpx.HTTPError, jwt.PyJWKClientError) as exc:
        # PyJWKClient fetches the key set with urllib, not httpx, and wraps a
        # connectivity failure in PyJWKClientConnectionError — which is not an
        # InvalidTokenError either. Without it an identity-provider outage
        # leaves this handler as an unhandled 500 rather than the 503 it is.
        logger.warning("JWKS fetch failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity provider unreachable")

    # The audience is checked by hand, after signature and issuer: the
    # webservice accepts the client ID in `aud` or in `azp`, and PyJWT only
    # knows the former — with its built-in check, the standard browser tokens
    # (azp, no aud) would all 401 here.
    if not _audience_ok(claims):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: required audience '{settings.keycloak_audience}' is missing",
        )

    try:
        subject = str(UUID(claims["sub"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    try:
        role = await active_user_role(subject)
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Logos authorization unavailable")
    return Principal(
        subject=subject,
        username=str(claims.get("preferred_username") or claims.get("email") or claims.get("sub", "")),
        roles=frozenset([role]) if role else frozenset(),
    )


async def require_agent_operator(
    principal: Principal = Depends(current_principal),
) -> Principal:
    if not principal.has_role(settings.required_role):
        raise HTTPException(status_code=403, detail=f"Logos role {settings.required_role} is required")
    return principal
