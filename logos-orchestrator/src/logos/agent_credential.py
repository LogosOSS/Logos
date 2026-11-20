"""One-minute inference credentials for the internal agent model gateway.

The database identity is stable for accounting; its stored key is never a
bearer credential. Only the secret-gated issuer can mint these tokens.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

AGENT_CREDENTIAL_PREFIX = "ac1."
AGENT_CREDENTIAL_TTL_S = 60


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def issue_agent_credential(api_key_id: int) -> str:
    secret = os.getenv("LOGOS_INTERNAL_SECRET", "")
    if not secret:
        raise RuntimeError("Internal agent credentials require LOGOS_INTERNAL_SECRET")
    now = int(time.time())
    payload = _encode(
        json.dumps(
            {
                "kid": api_key_id,
                "iat": now,
                "exp": now + AGENT_CREDENTIAL_TTL_S,
                "aud": "logos-agent-inference",
                "nonce": secrets.token_hex(16),
            },
            sort_keys=True,
        ).encode()
    )
    signature = _encode(
        hmac.new(secret.encode(), (AGENT_CREDENTIAL_PREFIX + payload).encode(), hashlib.sha256).digest()
    )
    return f"{AGENT_CREDENTIAL_PREFIX}{payload}.{signature}"


def resolve_agent_credential(credential: str) -> int | None:
    secret = os.getenv("LOGOS_INTERNAL_SECRET", "")
    if not secret or not credential.startswith(AGENT_CREDENTIAL_PREFIX) or len(credential) > 1024:
        return None
    try:
        payload, signature = credential[len(AGENT_CREDENTIAL_PREFIX) :].split(".")
        expected = _encode(
            hmac.new(secret.encode(), (AGENT_CREDENTIAL_PREFIX + payload).encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        kid, issued, expires = claims["kid"], claims["iat"], claims["exp"]
        if any(type(value) is not int for value in (kid, issued, expires)):
            return None
        if (
            kid <= 0
            or claims["aud"] != "logos-agent-inference"
            or expires - issued != AGENT_CREDENTIAL_TTL_S
            or not issued <= time.time() < expires
        ):
            return None
        return kid
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None
