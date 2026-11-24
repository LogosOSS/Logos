#!/usr/bin/env python3
"""Ask the runner to read or comment on this session's GitHub repository.

The session holds no GitHub credential. This command writes a request into
the session's artefact directory and waits for the runner's answer in the
read-only state directory. It works only for linked repositories, and
comments only where an administrator allowed them.

    logos-github issue 123
    logos-github issues --label proof --assignee none [--state open] [--page 2]
    logos-github comment 123 "Claiming"
    logos-github comment 123 - < note.md      (body from standard input)

The answer is printed as JSON; the exit code is 0 when the runner reports
success.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

REQUEST_DIR = "github-requests"
RESPONSE_DIR = "github"


def _request(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "issue":
        return {"action": "issue", "number": args.number}
    if args.command == "issues":
        request: dict[str, object] = {"action": "issues", "state": args.state, "page": args.page}
        if args.label:
            request["labels"] = args.label
        if args.assignee:
            request["assignee"] = args.assignee
        return request
    body = sys.stdin.read() if args.body == "-" else args.body
    if not body:
        raise SystemExit("a comment needs a body")
    return {"action": "comment", "number": args.number, "body": body}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="logos-github", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="read an issue or pull request with its comments")
    issue.add_argument("number", type=int)
    issues = commands.add_parser("issues", help="list issues and pull requests")
    issues.add_argument("--label", action="append", default=[], help="repeat for several labels (all must match)")
    issues.add_argument("--assignee", help="a login, 'none' for unassigned, '*' for any")
    issues.add_argument("--state", choices=["open", "closed", "all"], default="open")
    issues.add_argument("--page", type=int, default=1)
    comment = commands.add_parser("comment", help="comment on an issue or pull request")
    comment.add_argument("number", type=int)
    comment.add_argument("body", help="the comment, or - to read it from standard input")
    parser.add_argument("--timeout", type=float, default=180.0, help="seconds to wait for the runner")
    args = parser.parse_args(argv)

    artifacts = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts"))
    state = Path(os.environ.get("LOGOS_STATE_DIR", "/logos/state"))
    name = f"{uuid.uuid4().hex}.json"
    requests = artifacts / REQUEST_DIR
    requests.mkdir(parents=True, exist_ok=True)
    # Written under a name the runner ignores, then renamed: the runner never
    # reads half a request.
    partial = requests / f".{name}.partial"
    partial.write_text(json.dumps(_request(args)), encoding="utf-8")
    partial.rename(requests / name)

    answer = state / RESPONSE_DIR / name
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if answer.is_file():
            try:
                result = json.loads(answer.read_text(encoding="utf-8"))
            except ValueError:
                time.sleep(0.5)
                continue
            print(json.dumps(result, indent=2))
            return 0 if result.get("ok") else 1
        time.sleep(1.0)
    print(json.dumps({"ok": False, "error": "the runner did not answer in time; the request may still be carried out"}))
    return 2


if __name__ == "__main__":
    sys.exit(main())
