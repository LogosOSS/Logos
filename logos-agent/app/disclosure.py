"""What every piece of text the agent account publishes says about its origin.

A comment, a review, a reply or a pull request description written by a
session names the model that wrote it. Readers of a repository the agent
works in decide how much to trust a contribution, and the model is the first
thing they need for that. The footer is added by the runner where the text is
published, never by the agent, so a session cannot leave it out.
"""

from __future__ import annotations

import re

from .config import settings

_UNSAFE = re.compile(r"[`<>\r\n]")


def footer(*, model: str | None, session_id: int | None) -> str:
    """One line naming the account, the model and the session."""
    name = _UNSAFE.sub("", (model or "").strip())[:200] or "unknown model"
    parts = [f"🤖 Written by {settings.github_login} with the model `{name}`"]
    if session_id is not None:
        parts.append(f"Logos agent session {int(session_id)}")
    return "<sub>" + " · ".join(parts) + "</sub>"


def disclose(body: str, line: str) -> str:
    """The body with its footer, refusing to publish without one."""
    if not line.strip():
        raise ValueError("agent text must not be published without its model disclosure")
    return f"{body.rstrip()}\n\n{line}"
