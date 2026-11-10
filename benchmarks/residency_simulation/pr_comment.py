#!/usr/bin/env python
"""CI gate + PR comment for the GPU-less residency simulation.

Reads aggregated per-policy ``result.json`` (+ required ``junit.xml``) from
``LOGOS_SIM_REPORT_DIR`` (default: ``reports/`` next to this file) and:

  * with ``--check``: exits 0 only when every required policy report is present
    and gates PASS (fail-closed on missing / invalid / zero data);
  * without the flag: posts or edits one idempotent PR comment (HTML anchor),
    then exits with the same gate code. Comment API failures (fork read-only
    tokens) are logged and do not mask the gate.

Stdlib only. The workflow renders this report in a read-only aggregation job;
a separate job posts the resulting Markdown without executing repository code.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from report import (  # noqa: E402
    _ANCHOR,
    REQUIRED_POLICIES,
    build_markdown,
    gate_policy_report,
    load_aggregate,
    load_json,
    overall_verdict,
    parse_junit,
)

_API_TIMEOUT_S = 30.0


def _report_root() -> Path:
    return Path(os.environ.get("LOGOS_SIM_REPORT_DIR") or (_HERE / "reports"))


def _source_sha() -> str:
    return (os.environ.get("E2E_SIM_SOURCE_SHA") or os.environ.get("LOGOS_SIM_SOURCE_SHA") or "").strip()


def _run_url() -> str:
    explicit = os.environ.get("LOGOS_SIM_RUN_URL", "").strip()
    if explicit:
        return explicit
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""


def _api_request(method: str, url: str, token: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=_API_TIMEOUT_S) as response:
        body = response.read().decode()
        return json.loads(body) if body else {}


def _pr_number() -> Optional[str]:
    explicit = os.environ.get("LOGOS_BENCH_PR_NUMBER")
    if explicit:
        return explicit
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not Path(event_path).exists():
        return None
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    number = event.get("number") or (event.get("pull_request") or {}).get("number")
    return str(number) if number else None


def _is_own_comment(comment: Dict[str, Any]) -> bool:
    author = os.environ.get("LOGOS_BENCH_COMMENT_AUTHOR", "github-actions[bot]")
    return (comment.get("user") or {}).get("login") == author


def _find_last_bench_comment(api_comments: str, token: str) -> Optional[Dict[str, Any]]:
    latest: Optional[Dict[str, Any]] = None
    page = 1
    while True:
        comments = _api_request("GET", f"{api_comments}?per_page=100&page={page}", token)
        if not comments:
            return latest
        matching = [c for c in comments if _ANCHOR in (c.get("body") or "") and _is_own_comment(c)]
        if matching:
            latest = matching[-1]
        if len(comments) < 100:
            return latest
        page += 1


def _post_comment(markdown: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    server = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    number = _pr_number()
    if not token or not repo or not number:
        print("PR comment skipped (no GITHUB_TOKEN / GITHUB_REPOSITORY / PR number)")
        return
    api_comments = f"{server}/repos/{repo}/issues/{number}/comments"
    existing = _find_last_bench_comment(api_comments, token)
    if existing:
        _api_request(
            "PATCH",
            f"{server}/repos/{repo}/issues/comments/{existing['id']}",
            token,
            {"body": markdown},
        )
        print(f"PR comment updated (id={existing['id']})")
    else:
        _api_request("POST", api_comments, token, {"body": markdown})
        print("PR comment created")


def _single_policy_check(path: Path) -> int:
    """Gate one ``result.json`` (matrix leg ``--check --policy-file``)."""
    try:
        data = load_json(path)
    except Exception as exc:  # noqa: BLE001 — surface as GATE FAIL
        print(f"GATE FAIL: {exc}")
        return 1
    junit = None
    junit_path = path.with_name("junit.xml")
    if junit_path.is_file():
        try:
            junit = parse_junit(junit_path)
        except Exception as exc:  # noqa: BLE001
            print(f"GATE FAIL: {exc}")
            return 1
    expected_policy = os.environ.get("E2E_RESIDENCY_POLICY") or None
    verdict, reasons = gate_policy_report(data, junit, expected_policy=expected_policy)
    if verdict != "PASS":
        print(f"GATE FAIL ({data.get('residency_policy', '?')}): " + "; ".join(reasons))
        return 1
    print(f"GATE PASS ({data.get('residency_policy')}): functional simulation OK")
    return 0


def _render_and_gate(root: Path) -> tuple[str, int]:
    reports, junit_by_policy, reasons = load_aggregate(root)
    overall = overall_verdict(reports, junit_by_policy)
    if any(reasons.get(policy) for policy in REQUIRED_POLICIES):
        overall = "FAIL"
    markdown = build_markdown(
        reports,
        policy_reasons=reasons,
        junit_by_policy=junit_by_policy,
        source_sha=_source_sha(),
        run_url=_run_url(),
        overall=overall,
    )
    if overall == "PASS":
        print("GATE PASS: all residency policies produced valid synthetic reports")
        return markdown, 0
    detail: List[str] = []
    for policy in REQUIRED_POLICIES:
        if reasons.get(policy):
            detail.append(f"{policy}: " + "; ".join(reasons[policy]))
        elif reports.get(policy) is None:
            detail.append(f"{policy}: report missing")
    print("GATE FAIL: " + (" | ".join(detail) if detail else "incomplete aggregate"))
    return markdown, 1


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    if "--policy-file" in args:
        idx = args.index("--policy-file")
        if idx + 1 >= len(args):
            print("GATE FAIL: --policy-file requires a path")
            return 1
        return _single_policy_check(Path(args[idx + 1]))

    root = _report_root()
    markdown, code = _render_and_gate(root)

    if check_only:
        return code

    if os.environ.get("LOGOS_BENCH_PR_COMMENT_DRY_RUN") == "1":
        print(markdown)
        return code

    try:
        _post_comment(markdown)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        print(f"PR comment failed (continuing): {exc}")
    return code


if __name__ == "__main__":
    sys.exit(main())
