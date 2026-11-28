"""Trusted identifiers for automatically refreshed workflow analyses."""

import hashlib
import json
import re
from typing import Any

MAX_TARGET_ATTEMPTS = 3


def commit_sha(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None


def link_scope(link: dict[str, Any]) -> str:
    paths = link.get("paths") or []
    scope = [str(link["repo_slug"]).lower(), str(link.get("branch") or "main"), sorted(paths)]
    return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
