#!/usr/bin/env python3
"""Render only the credentials required by one deployment target."""

import json
import os
import sys
from collections.abc import Mapping

CORE_SECRETS = (
    "LOGOS_DB_PASSWORD",
    "LOGOS_REDIS_PASSWORD",
    "LOGOS_INTERNAL_SECRET",
    "LOGOS_REPO_CREDENTIALS_KEY",
    "LOGOS_AGENT_API_KEY",
    "LOGOS_AGENT_GITHUB_TOKEN",
    "LOGOS_AGENT_SESSION_GITHUB_TOKEN",
    "KEYCLOAK_SYNC_CLIENT_SECRET",
    "PROMETHEUS_API_KEY",
    "HF_TOKEN",
)
WORKER_SECRETS = ("LOGOS_API_KEY", "HF_TOKEN")


def render_secrets(target: str, environment: Mapping[str, str]) -> str:
    """Return Compose dotenv lines; new repository secrets are never included."""
    if target not in {"core", "worker"}:
        raise ValueError("Unknown deployment target")
    names = CORE_SECRETS if target == "core" else WORKER_SECRETS
    required = {"LOGOS_DB_PASSWORD", "LOGOS_REDIS_PASSWORD"} if target == "core" else {"LOGOS_API_KEY"}
    lines = []
    for name in names:
        value = environment.get("DEPLOY_SECRET_" + name, "")
        if not value:
            if name in required:
                raise ValueError(f"Required deployment secret {name} is empty")
            continue
        if any(char in value for char in "\r\n\0"):
            raise ValueError(f"Deployment secret {name} must be a single line")
        # Compose expands dollars inside double quotes. Escape them before
        # JSON quoting, which also preserves quotes and trailing backslashes.
        quoted = json.dumps(value.replace("$", "$$"), ensure_ascii=False)
        lines.append(f"{name}={quoted}\n")
    return "".join(lines)


if __name__ == "__main__":
    try:
        sys.stdout.write(render_secrets(sys.argv[1], os.environ))
    except ValueError as exc:
        sys.exit(str(exc))
