"""GitHub actions a running session asks the runner to take on its behalf.

The agent phase holds no GitHub credential. Some repositories still expect
a contributor to talk on an issue while working on it: to claim the issue
before starting, to release it with a note, to report that a statement is
wrong. A session asks for such an action by writing a small JSON request
into its artefact directory; the runner, which holds the token, validates
the request and carries it out against the session's own repository only.
The answer goes into the session's state directory, which the session can
read and cannot write, so a link planted by the agent cannot redirect the
runner's write.

What a request can do is deliberately small:

* ``issue`` and ``issues`` read an issue with its conversation, or list
  issues by label, state and assignee. Every linked-repository session may
  read: the repository is already checked out in its container.
* ``comment`` posts a comment on an issue or pull request. Only when an
  administrator allowed it for the repository, bounded per session, with
  mentions neutralised and the model disclosure appended.

No labels, no assignment, no closing, no other repository. Every action is
recorded as an event of the session, so what the agent said in public is
visible on its page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import disclosure, github
from .artifacts import read_artifact_text
from .config import settings

logger = logging.getLogger(__name__)

REQUEST_DIR = "github-requests"
RESPONSE_DIR = "github"
MAX_REQUEST_BYTES = 64 * 1024
MAX_COMMENTS_PER_SESSION = 30
MAX_READS_PER_SESSION = 500
MAX_REQUESTS_PER_PASS = 10
MAX_COMMENT_CHARS = 20000
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}\.json$")
_LABEL = re.compile(r"^[^,\r\n]{1,100}$")
_LOGIN = re.compile(r"^(none|\*|[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$")
# A mention notifies a person. Text written by an agent that may have read
# instructions planted in an issue must not page people, so an at sign in
# front of a name is separated from it by a zero-width space.
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["comment", "issue", "issues"]
    number: int | None = Field(default=None, ge=1, le=10**9)
    body: str | None = Field(default=None, min_length=1, max_length=MAX_COMMENT_CHARS)
    labels: list[str] = Field(default_factory=list, max_length=10)
    state: Literal["open", "closed", "all"] = "open"
    assignee: str | None = None
    page: int = Field(default=1, ge=1, le=50)


def _neutralise_mentions(body: str) -> str:
    return _MENTION.sub("@​", body)


def _requests(artifacts: Path) -> list[str]:
    """Names of request files, without following a planted link."""
    try:
        descriptor = os.open(artifacts / REQUEST_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        logger.info("cannot read %s: %s", artifacts / REQUEST_DIR, exc)
        return []
    try:
        return sorted(name for name in os.listdir(descriptor) if _NAME.match(name))
    finally:
        os.close(descriptor)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output)
        # Readable by the session user only. The state directory is mounted
        # read-only into the session, so owning the file lets it read the
        # answer, not change it. Where the runner cannot hand files over (an
        # unprivileged development runner), the file stays the runner's.
        try:
            os.chown(temporary, settings.session_uid, settings.session_uid)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _count(responses: Path, action: str) -> int:
    if not responses.is_dir():
        return 0
    total = 0
    for path in responses.glob("*.json"):
        try:
            if json.loads(path.read_text()).get("action") == action:
                total += 1
        except (OSError, ValueError):
            continue
    return total


async def process(
    session: dict[str, Any],
    *,
    artifacts: Path,
    state: Path,
    may_comment: bool,
    record: Any,
) -> int:
    """Carry out the session's outstanding requests. Returns how many were answered.

    ``record`` is an async callable receiving one line for the session's
    transcript. The caller scopes the GitHub client to the session's
    repository; nothing in a request can name another one.
    """
    responses = state / RESPONSE_DIR
    answered = 0
    for name in _requests(artifacts):
        if answered >= MAX_REQUESTS_PER_PASS:
            break
        response_path = responses / name
        pending_path = responses / f"{name}.pending"
        if response_path.exists():
            continue
        if pending_path.exists():
            # A previous pass started this request and did not finish it:
            # repeating a comment could say it twice.
            _write_json(
                response_path,
                {"ok": False, "action": "unknown", "error": "the runner was interrupted; check before repeating"},
            )
            pending_path.unlink(missing_ok=True)
            answered += 1
            continue
        try:
            raw = read_artifact_text(artifacts, f"{REQUEST_DIR}/{name}", limit=MAX_REQUEST_BYTES)
            request = Request.model_validate(json.loads(raw))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, ValidationError) as exc:
            _write_json(response_path, {"ok": False, "action": "invalid", "error": f"invalid request: {exc}"[:2000]})
            answered += 1
            continue
        result = await _carry_out(request, session, responses, pending_path, may_comment, record)
        _write_json(response_path, result)
        pending_path.unlink(missing_ok=True)
        answered += 1
    return answered


async def _carry_out(
    request: Request,
    session: dict[str, Any],
    responses: Path,
    pending_path: Path,
    may_comment: bool,
    record: Any,
) -> dict[str, Any]:
    action = request.action
    try:
        if action == "comment":
            if not may_comment:
                return {"ok": False, "action": action, "error": "comments are not allowed for this repository"}
            if request.number is None or request.body is None:
                return {"ok": False, "action": action, "error": "a comment needs a number and a body"}
            if _count(responses, "comment") >= MAX_COMMENTS_PER_SESSION:
                return {"ok": False, "action": action, "error": "this session has used all its comments"}
            _write_json(pending_path, {"action": action})
            footer = disclosure.footer(model=session.get("model"), session_id=int(session["id"]))
            url = await github.post_issue_comment(request.number, _neutralise_mentions(request.body), footer=footer)
            await record(f"[github] commented on #{request.number}: {url}")
            return {"ok": True, "action": action, "url": url}
        if _count(responses, "issue") + _count(responses, "issues") >= MAX_READS_PER_SESSION:
            return {"ok": False, "action": action, "error": "this session has used all its reads"}
        if action == "issue":
            if request.number is None:
                return {"ok": False, "action": action, "error": "an issue read needs a number"}
            issue = await github.issue_snapshot(request.number)
            await record(f"[github] read #{request.number}")
            return {"ok": True, "action": action, "issue": issue}
        labels = [label.strip() for label in request.labels if label.strip()]
        if any(not _LABEL.match(label) for label in labels):
            return {"ok": False, "action": action, "error": "labels must not contain commas or line breaks"}
        if request.assignee is not None and not _LOGIN.match(request.assignee):
            return {"ok": False, "action": action, "error": "assignee must be a login, 'none' or '*'"}
        issues = await github.list_issues(
            labels=labels, state=request.state, assignee=request.assignee, page=request.page
        )
        await record(f"[github] listed issues (labels={labels}, state={request.state}, page={request.page})")
        return {"ok": True, "action": action, "issues": issues}
    except github.GitHubError as exc:
        return {"ok": False, "action": action, "error": str(exc)[:2000]}
