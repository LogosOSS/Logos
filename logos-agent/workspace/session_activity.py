"""Presentation metadata from the root Claude Code stream and its tool hooks."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

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
        directory = Path(os.environ.get("LOGOS_ARTIFACT_DIR", "/artifacts"))
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as out:
                json.dump(self.data, out)
            os.replace(out.name, directory / "activity.json")
        except OSError:
            pass  # A missing estimate must never interrupt a coding session.


if __name__ == "__main__":
    try:
        result = remind(json.load(sys.stdin), Path(tempfile.gettempdir()) / "logos-progress-reminder")
        if result:
            print(json.dumps(result))
    except (OSError, ValueError):
        pass
