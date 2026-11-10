"""Workflow-tag attribution and SLO to queue-priority mapping.

Applications send ``X-Logos-Workflow-Tag`` so Logos looks up the matching
workflow step and applies the SLO stored for it. A request cannot name its own
SLO. Precedence over the key/team/policy chain is defined by
``resolve_request_priority``.
"""

from __future__ import annotations

from typing import Mapping, Optional

VALID_SLOS = frozenset({"ux-critical", "ux-high-prio", "ux-background"})

_SLO_TO_PRIORITY = {
    "ux-critical": 10,
    "ux-high-prio": 5,
    "ux-background": 1,
}

_WORKFLOW_TAG_HEADER_NAMES = ("x-logos-workflow-tag", "logos-workflow-tag")


def slo_to_priority(slo: str) -> int:
    """Map an SLO string to the 1/5/10 queue priority scale.

    Args:
        slo: One of ``VALID_SLOS``.

    Returns:
        10 for ux-critical, 5 for ux-high-prio, 1 for ux-background.

    Raises:
        KeyError: When ``slo`` is not a known SLO string.
    """
    return _SLO_TO_PRIORITY[slo]


def _header_value(headers: Mapping[str, str], names: tuple[str, ...]) -> Optional[str]:
    """Return the first non-empty header value among ``names`` (case-insensitive)."""
    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def parse_workflow_tag_header(headers: Optional[Mapping[str, str]]) -> Optional[str]:
    """Parse ``X-Logos-Workflow-Tag`` (or ``logos-workflow-tag``).

    Args:
        headers: Request headers (case-insensitive).

    Returns:
        The trimmed tag string, or None when absent.
    """
    return _header_value(headers or {}, _WORKFLOW_TAG_HEADER_NAMES)


def resolve_request_priority(
    tag_slo: Optional[str],
    default_priority: Optional[int],
    team_priority: Optional[int],
    policy_priority: Optional[int],
) -> int:
    """Resolve queue priority with the workflow-step SLO above the key/team/policy chain.

    Precedence: workflow-tag step SLO > API key ``default_priority`` > team
    priority > policy priority.

    Args:
        tag_slo: SLO from a workflow-step tag lookup, or None.
        default_priority: Key owner's configured priority (0 = unset).
        team_priority: Team admin priority (0 = unset).
        policy_priority: Policy-level priority (may be 0/None).

    Returns:
        Effective integer priority on the 1/5/10 scale (or NORMAL when unset).
    """
    if tag_slo:
        return slo_to_priority(tag_slo)
    # Lazy import: pipeline imports this module at classify time.
    from logos.pipeline.pipeline import resolve_queue_priority

    return resolve_queue_priority(default_priority, team_priority, policy_priority)
