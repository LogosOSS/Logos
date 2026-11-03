"""The notification for the end of a node's deployment.

A node manager reports how a deployment ended. This module turns that report into the arguments of
``emit_notification``. Only the ending is worth an inbox item, whatever it is: an admin who waits for the result of a
deployment should get one. The phases in between stay in the log of the node manager.

The inbox shows the body in two lines, so the title and the body carry only what a person needs at a glance. The full
message of the node manager is part of the payload.
"""

from __future__ import annotations

import re
from typing import Any

from logos.dbutils.dbrequest import LogosNodeDeploymentReport

# The title column of the application server holds 255 characters.
_MAX_TITLE_CHARS = 255
_LOGOS_ADMINS = [{"kind": "role", "role": "logos_admin"}]
# The endings that a person has to look at. A deployment that replaced nothing is a success as well.
_FAILED = ("rolled_back", "failed")
# What fits the two lines of the inbox next to the line about the requester.
_MAX_REASON_CHARS = 100
_MAX_REQUESTER_CHARS = 60
_CI_RUN = re.compile(r"/runs/(\d+)")


def _drain_sentence(report: LogosNodeDeploymentReport) -> str:
    if report.drained_requests is None or report.drain_seconds is None:
        return ""
    if report.drained_requests == 0:
        return f"No requests were running; drained in {report.drain_seconds} s."
    verb = "request was" if report.drained_requests == 1 else "requests were"
    return f"{report.drained_requests} {verb} running; drained in {report.drain_seconds} s."


def _models_note(report: LogosNodeDeploymentReport) -> str:
    count = len(report.sole_replica_models)
    if not count:
        return ""
    return f"{count} model had no other node" if count == 1 else f"{count} models had no other node"


def _requester(report: LogosNodeDeploymentReport) -> str:
    """A CI run as ``run <id>``; any other text as it is, cut to a line."""
    text = report.requested_by.strip()
    run = _CI_RUN.search(text)
    return f"run {run.group(1)}" if run else text[:_MAX_REQUESTER_CHARS]


def _reason(report: LogosNodeDeploymentReport) -> str:
    """The first sentence of the message of the node manager. The rest is usually a command's output or advice."""
    first = re.split(r"(?<=[.!?])\s|\n", report.message.strip(), maxsplit=1)[0]
    if len(first) > _MAX_REASON_CHARS:
        first = first[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return first


def _title(node: str, report: LogosNodeDeploymentReport) -> str:
    if report.outcome == "succeeded":
        title = f"{node} now runs {report.tag}"
    elif report.outcome == "unchanged":
        title = f"{node} already runs {report.tag}"
    elif report.outcome == "rolled_back":
        title = f"{node} was rolled back after deploying {report.tag}"
    elif report.failure == "rollback_failed":
        title = f"{node} needs manual action: deploying {report.tag} and its rollback failed"
    else:
        title = f"Deploying {report.tag} on {node} failed"
    return title[:_MAX_TITLE_CHARS]


def _body(report: LogosNodeDeploymentReport) -> str | None:
    if report.outcome in _FAILED:
        lines = [_reason(report), _requester(report)]
    else:
        # The title already says which image runs.
        facts = " · ".join(part for part in (_models_note(report), _requester(report)) if part)
        lines = [_drain_sentence(report), facts]
    return "\n".join(line for line in lines if line) or None


def deployment_notification(node: str, report: LogosNodeDeploymentReport) -> dict[str, Any]:
    """The arguments of ``emit_notification`` for a reported ending, addressed to the Logos admins."""
    return {
        "notification_type": "deployment_failed" if report.outcome in _FAILED else "deployment",
        "title": _title(node, report),
        "body": _body(report),
        "payload": {
            "node": node,
            "deployment_id": report.deployment_id,
            "tag": report.tag,
            "outcome": report.outcome,
            "failure": report.failure,
            "message": report.message,
            "drained_requests": report.drained_requests,
            "drain_seconds": report.drain_seconds,
            "sole_replica_models": report.sole_replica_models,
            "requested_by": report.requested_by,
        },
        "targets": _LOGOS_ADMINS,
    }
