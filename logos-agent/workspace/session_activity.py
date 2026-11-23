"""Presentation metadata from the root Claude Code stream and its tool hooks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlsplit

INTERVAL = 600
REPORT = re.compile(r"Logos progress: (100|[1-9]?[0-9])%")
REMINDER = (
    "Briefly estimate overall completion of the current Logos task. Include a standalone line "
    "'Logos progress: N%' (integer 0–100) in your next assistant response, then continue working."
)


def active_seconds() -> int | None:
    """The runner moves this origin forward by each pause, including across restarts."""
    try:
        origin = float((Path(os.environ.get("LOGOS_STATE_DIR", "/logos/state")) / "active-origin").read_text())
        return max(0, int(time.time() - origin))
    except (OSError, ValueError, OverflowError):
        return None


def remind(event: dict, marker: Path) -> dict | None:
    if (
        event.get("agent_id")
        or event.get("agent_type")
        or event.get("hook_event_name")
        not in {
            "PostToolUse",
            "PostToolUseFailure",
        }
    ):
        return None
    active = active_seconds()
    if active is None or active < INTERVAL:
        return None
    # Root tool calls may finish concurrently. All continuations in this
    # container share the last reminder, even if a model hasn't answered it.
    with marker.open("a+") as state:
        fcntl.flock(state, fcntl.LOCK_EX)
        state.seek(0)
        if active - int(state.read() or 0) < INTERVAL:
            return None
        state.seek(0)
        state.truncate()
        state.write(str(active))
    return {"hookSpecificOutput": {"hookEventName": event["hook_event_name"], "additionalContext": REMINDER}}


class SessionActivity:
    def __init__(self):
        self.messages = AssistantMessages()
        self.children: set[str] = set()
        self.reports: set[str] = set()
        self.calls: set[tuple[str, str]] = set()
        self.starts: dict[tuple[str, str], str] = {}
        self.data = {}

    def observe(self, event: dict, root: str) -> None:
        if not root or event.get("session_id") != root or event.get("isReplay"):
            return
        if (
            event.get("type") == "system"
            and event.get("subtype") == "task_started"
            and event.get("task_type") == "local_agent"
        ):
            child, call = event.get("task_id"), event.get("tool_use_id")
            if isinstance(child, str) and child and isinstance(call, str) and call:
                self.starts[(root, call)] = child
                self._record_child(root, call)
        if event.get("type") == "assistant":
            parent = event.get("parent_tool_use_id")
            if parent is None or isinstance(parent, str):
                self.messages.observe(event, root, self.starts.get((root, parent)))
            message = event.get("message") or {}
            for block in message.get("content", []):
                if (
                    block.get("type") == "tool_use"
                    and block.get("name") in {"Agent", "Task", "SendMessage"}
                    and block.get("id")
                ):
                    call = block["id"]
                    self.calls.add((root, call))
                    self._record_child(root, call)
            if "parent_tool_use_id" not in event or event["parent_tool_use_id"] is not None or event.get("agent_id"):
                return
            for block in message.get("content", []):
                if block.get("type") != "text":
                    continue
                for line in block.get("text", "").splitlines():
                    report = REPORT.fullmatch(line.strip())
                    identity = message.get("id")
                    if report and identity and identity not in self.reports:
                        self.reports.add(identity)
                        active = active_seconds()
                        if active is not None:
                            self.data.update(
                                progress_percent=int(report[1]),
                                progress_active_s=active,
                                progress_reported_us=time.time_ns() // 1000,
                            )
                            self.write()
                        return

    def _record_child(self, root: str, call: str) -> None:
        key = (root, call)
        child = self.starts.get(key)
        if key in self.calls and child and child not in self.children:
            self.children.add(child)
            self.data["subagent_count"] = len(self.children)
            self.write()

    def write(self):
        write_snapshot("activity.json", self.data)


def write_snapshot(name: str, data: dict) -> None:
    """Publish the complete snapshot even when a reader is active during the write."""
    directory = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts"))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as out:
            temporary = Path(out.name)
            json.dump(data, out, ensure_ascii=False)
        os.replace(temporary, directory / name)
    except (OSError, UnicodeError):
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def redact_output(content: str) -> str:
    """Remove session credentials before persisting visible output."""
    secrets = [os.environ.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")]
    for key in ("HTTPS_PROXY", "https_proxy"):
        try:
            password = urlsplit(os.environ.get(key, "")).password
            if password:
                secrets.append(unquote(password))
        except ValueError:
            continue
    for secret in secrets:
        if secret:
            content = content.replace(secret, "***")
    content = re.sub(r"(://[^/\s:@]+:)[^@/\s]+@", r"\1***@", content)
    content = re.sub(r"(?i)(\bauthorization\s*:\s*(?:bearer|basic)\s+)[A-Za-z0-9+/=._~-]+", r"\1***", content)
    content = re.sub(r"(?i)(\bbearer\s+)[A-Za-z0-9+/=._~-]{16,}", r"\1***", content)
    content = re.sub(r"\b(?:gh[pousr]_|github_pat_|sk-(?:ant-)?)[A-Za-z0-9_-]{16,}", "***", content)
    return content


class AssistantMessages:
    """A bounded snapshot of visible assistant blocks, shared by CLI continuations."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.seen: deque[str] = deque(maxlen=512)
        self.omitted = 0

    def observe(self, event: dict, root: str, child: str | None) -> None:
        if event.get("type") != "assistant" or event.get("isMeta") or event.get("isApiErrorMessage"):
            return
        message = event.get("message") or {}
        if message.get("role") != "assistant" or not message.get("id") or message.get("model") == "<synthetic>":
            return
        if "parent_tool_use_id" not in event:
            return
        parent = event.get("agent_id") or child or event["parent_tool_use_id"]
        actor = "main" if parent is None else "child-" + hashlib.sha256(str(parent).encode()).hexdigest()[:8]
        visible = [
            b["text"] for b in message.get("content", []) if b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        content = "\n\n".join(visible)
        if not content.strip():
            return
        content = redact_output(content).encode("utf-8", errors="replace").decode()
        key = hashlib.sha256(json.dumps([root, actor, message["id"]]).encode()).hexdigest()[:32]
        # The CLI can emit successive content blocks under one model message ID.
        # Each emitted envelope has its own UUID. Result summaries can repeat text.
        identity = event.get("uuid") or hashlib.sha256(content.encode()).hexdigest()
        envelope = hashlib.sha256(f"{key}/{identity}".encode()).hexdigest()
        if envelope in self.seen:
            return
        self.seen.append(envelope)
        row = self.rows.setdefault(key, {"id": key, "actor": actor, "text": "", "truncated": False})
        if row["truncated"]:
            return
        joined = (row["text"] + "\n\n" if row["text"] else "") + content
        row["truncated"] = len(joined) > 16384
        row["text"] = joined[:16384]
        while len(self.rows) > 64 or sum(len(r["text"]) for r in self.rows.values()) > 65536:
            del self.rows[next(iter(self.rows))]
            self.omitted += 1
        write_snapshot("messages.json", {"messages": list(self.rows.values()), "omitted": self.omitted})


if __name__ == "__main__":
    try:
        result = remind(json.load(sys.stdin), Path(tempfile.gettempdir()) / "logos-progress-reminder")
        if result:
            print(json.dumps(result))
    except (OSError, ValueError):
        pass
