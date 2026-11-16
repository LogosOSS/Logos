"""The per-request reuse context: which stored answers a request may see.

A client opts into answer reuse by sending a ``logos-context`` header naming
the context its question belongs to, for example a shop, a course or one
user within a course. Requests without the header never read or write the
answer cache.

The header value is chosen by the client, so on its own it is not a safe
boundary: two teams could both send ``shop-a``. The stored scope therefore
also names the owner of the API key (its team, or the key itself for keys
without a team), and a request can only ever reach its own owner's contexts.
"""

from __future__ import annotations

from typing import Dict, Optional

CONTEXT_HEADER = "logos-context"
# Both spellings are accepted, as for the logos-logging header.
CONTEXT_HEADER_ALT = "logos_context"

MAX_CONTEXT_ID_LENGTH = 200


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    """Return the value of ``name`` in ``headers``, matching names in any letter case."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


class InvalidContextId(ValueError):
    """The ``logos-context`` header is present but not a usable context name."""


def context_id_from_headers(headers: Optional[Dict[str, str]]) -> Optional[str]:
    """Read the client's reuse context from the request headers.

    Params:
        headers: The request headers (any letter case).

    Returns:
        The trimmed context name, or None when the header is absent or blank
        (the request takes no part in answer reuse).

    Raises:
        InvalidContextId: The value is longer than
            :data:`MAX_CONTEXT_ID_LENGTH` or contains control characters.
    """
    headers = headers or {}
    raw = _header(headers, CONTEXT_HEADER)
    if raw is None:
        raw = _header(headers, CONTEXT_HEADER_ALT)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if len(value) > MAX_CONTEXT_ID_LENGTH:
        raise InvalidContextId(f"{CONTEXT_HEADER} must be at most {MAX_CONTEXT_ID_LENGTH} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise InvalidContextId(f"{CONTEXT_HEADER} must not contain control characters")
    return value


def cache_scope(context_id: str, team_id: Optional[int], api_key_id: int) -> str:
    """Return the scope under which a context's answers are stored.

    The scope is what the ``context_id`` column of answer_cache holds. It
    combines the client's context name with the key's owner, so equal names
    from different owners never share answers.

    Params:
        context_id: The name from the ``logos-context`` header.
        team_id: The team owning the API key, or None.
        api_key_id: The API key, used as the owner when it has no team.

    Returns:
        ``"team:<id>:<context>"``, or ``"key:<id>:<context>"`` without a team.
    """
    owner = f"team:{team_id}" if team_id is not None else f"key:{api_key_id}"
    return f"{owner}:{context_id}"
