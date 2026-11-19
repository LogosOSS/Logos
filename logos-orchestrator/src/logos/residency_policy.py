"""Shared residency strategy selection for planning and routing."""

import logging
import os

DEFAULT_RESIDENCY_POLICY = "simple"
RESIDENCY_POLICIES = ("demand", "lru", "simple")


def parse_residency_policy(raw: str | None) -> str:
    """Normalize the configured strategy, using simple for missing, empty or unknown values."""
    value = (raw or DEFAULT_RESIDENCY_POLICY).strip().lower()
    if value not in RESIDENCY_POLICIES:
        logging.getLogger(__name__).warning(
            "Unknown LOGOS_RESIDENCY_POLICY=%r; falling back to %r (valid: %s)",
            raw,
            DEFAULT_RESIDENCY_POLICY,
            ", ".join(RESIDENCY_POLICIES),
        )
        return DEFAULT_RESIDENCY_POLICY
    return value


def configured_residency_policy() -> str:
    """Read the strategy once when a routing or planning component starts."""
    return parse_residency_policy(os.environ.get("LOGOS_RESIDENCY_POLICY"))
