"""Signed tokens establish identity. The current Logos role authorizes the operator."""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import auth
from app.auth import require_agent_operator
from fastapi import HTTPException, Request


def _settings(monkeypatch, **overrides):
    patched = replace(auth.settings, **overrides)
    monkeypatch.setattr(auth, "settings", patched)
    return patched


class TestTokenAudience:
    """Signed-token validation across the claim shapes Keycloak emits.

    The standard browser flow (authorization code) puts the authorized
    party in ``azp`` and leaves ``aud`` absent; other flows carry it in
    ``aud``. The webservice's resource server accepts the configured
    client ID in either place, and this service must too — otherwise
    every administrator who can use the rest of Logos gets a 401 from
    the agent endpoints. These tests run the full verification path:
    signature against a JWKS stand-in, issuer, and the audience check.
    """

    ISSUER = "https://keycloak.example/realms/logos"

    @pytest.fixture
    def signer(self, monkeypatch):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        class _JwksStandIn:
            def get_signing_key_from_jwt(self, _token):
                return SimpleNamespace(key=private.public_key())

        _settings(
            monkeypatch,
            keycloak_audience="logos",
            keycloak_issuer_uri=self.ISSUER,
            keycloak_jwks_uri=f"{self.ISSUER}/protocol/openid-connect/certs",
        )
        monkeypatch.setattr(auth, "_jwk_client", _JwksStandIn())
        monkeypatch.setattr(auth, "_jwk_client_created_at", time.monotonic())
        monkeypatch.setattr(auth, "active_user_role", AsyncMock(return_value="logos_admin"))
        return private

    def _token(self, signer, **claims) -> str:
        import jwt as pyjwt

        now = int(time.time())
        payload = {
            "iss": self.ISSUER,
            "sub": "00000000-0000-0000-0000-000000000001",
            "preferred_username": "alice",
            "realm_access": {"roles": ["itg-admin"]},
            "iat": now,
            "exp": now + 3600,
            **claims,
        }
        return pyjwt.encode(payload, signer, algorithm="RS256")

    @staticmethod
    def _request(token: str) -> Request:
        return Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})

    async def test_a_token_with_the_client_id_in_aud_is_accepted(self, monkeypatch, signer):
        token = self._token(signer, aud="logos")
        principal = await auth.current_principal(self._request(token))
        assert auth.settings.required_role in principal.roles

    async def test_a_token_with_the_client_id_in_azp_is_accepted(self, monkeypatch, signer):
        # The standard Keycloak browser shape: azp carries the client,
        # aud is absent. Signature and issuer still verified.
        token = self._token(signer, azp="logos")
        principal = await auth.current_principal(self._request(token))
        assert auth.settings.required_role in principal.roles

    async def test_a_token_with_the_client_id_in_an_aud_list_is_accepted(self, monkeypatch, signer):
        token = self._token(signer, aud=["some-tenant", "logos"])
        principal = await auth.current_principal(self._request(token))
        assert auth.settings.required_role in principal.roles

    async def test_demotion_removes_access_with_the_same_signed_token(self, monkeypatch, signer):
        request = self._request(self._token(signer, azp="logos"))
        monkeypatch.setattr(auth, "active_user_role", AsyncMock(side_effect=["logos_admin", "app_developer", None]))
        await require_agent_operator(await auth.current_principal(request))
        for _ in range(2):
            with pytest.raises(HTTPException) as error:
                await require_agent_operator(await auth.current_principal(request))
            assert error.value.status_code == 403

    async def test_a_token_without_the_client_id_in_aud_or_azp_is_rejected(self, monkeypatch, signer):
        token = self._token(signer, aud="other-client", azp="other-client")
        with pytest.raises(HTTPException) as excinfo:
            await auth.current_principal(self._request(token))
        assert excinfo.value.status_code == 401
        assert "audience" in excinfo.value.detail

    async def test_a_token_with_no_audience_claims_at_all_is_rejected(self, monkeypatch, signer):
        token = self._token(signer)
        with pytest.raises(HTTPException) as excinfo:
            await auth.current_principal(self._request(token))
        assert excinfo.value.status_code == 401

    async def test_a_token_for_a_different_issuer_is_still_rejected(self, monkeypatch, signer):
        # The audience work must not have loosened the rest of the
        # verification: a correctly signed token from another issuer is
        # still no good.
        import jwt as pyjwt

        now = int(time.time())
        token = pyjwt.encode(
            {
                "iss": "https://other.example/realms/other",
                "sub": "00000000-0000-0000-0000-000000000001",
                "azp": "logos",
                "exp": now + 3600,
            },
            signer,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as excinfo:
            await auth.current_principal(self._request(token))
        assert excinfo.value.status_code == 401
