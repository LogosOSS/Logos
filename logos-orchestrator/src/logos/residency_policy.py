"""Shared residency strategy selection for planning and routing."""

import logging
import os

RESIDENCY_POLICIES = ("demand", "lru", "simple")


def parse_residency_policy(raw: str | None) -> str:
    """Normalize the configured strategy, retaining demand for unknown values."""
    value = (raw or "demand").strip().lower()
    if value not in RESIDENCY_POLICIES:
        logging.getLogger(__name__).warning(
            "Unknown LOGOS_RESIDENCY_POLICY=%r; falling back to 'demand' (valid: %s)",
            raw,
            ", ".join(RESIDENCY_POLICIES),
        )
        return "demand"
    return value


def configured_residency_policy() -> str:
    """Read the strategy once when a routing or planning component starts."""
    return parse_residency_policy(os.environ.get("LOGOS_RESIDENCY_POLICY"))
