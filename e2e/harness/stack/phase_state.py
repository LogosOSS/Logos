"""Cross-phase observation state for the external phased benchmark runner.

Traffic pytest writes real counts only after validation. Lifecycle pytest loads
and checks the same invocation / source SHA / residency policy. Never forge
expected pattern totals.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness.sim_report import git_head_sha, residency_policy
from harness.workload import PatternReport, RequestResult

STATE_VERSION = 1
ENV_STATE_PATH = "E2E_SIM_PHASE_STATE"
ENV_INVOCATION_ID = "E2E_SIM_INVOCATION_ID"
ENV_LIFECYCLE_PREPARED = "E2E_LIFECYCLE_PREPARED"


def new_invocation_id() -> str:
    return uuid.uuid4().hex


def default_state_path(work_dir: Path, invocation_id: str) -> Path:
    return Path(work_dir) / f"phase-state-{invocation_id}.json"


def serialize_pattern_reports(reports: list[PatternReport]) -> list[dict[str, Any]]:
    return [
        {
            "pattern": r.pattern,
            "results": [asdict(item) for item in r.results],
        }
        for r in reports
    ]


def deserialize_pattern_reports(raw: list[dict[str, Any]]) -> list[PatternReport]:
    out: list[PatternReport] = []
    for block in raw:
        results = [RequestResult(**item) for item in (block.get("results") or [])]
        out.append(PatternReport(pattern=str(block["pattern"]), results=results))
    return out


def build_traffic_state(
    *,
    invocation_id: str,
    pattern_reports: list[PatternReport],
    cold_load: int,
    vram_samples: int,
    vram_violations: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble state from *validated* traffic observations (caller must assert)."""
    state: dict[str, Any] = {
        "state_version": STATE_VERSION,
        "invocation_id": str(invocation_id),
        "source_sha": git_head_sha(),
        "residency_policy": residency_policy(),
        "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "phase": "traffic",
        "observations": {
            "pattern_reports": serialize_pattern_reports(pattern_reports),
            "cold_load": int(cold_load),
            "vram_samples": int(vram_samples),
            "vram_violations": int(vram_violations),
            "sleep": 0,
            "wake": 0,
            "wake_same_pid": False,
        },
    }
    if extra:
        state["extra"] = dict(extra)
    return state


def write_state(state: dict[str, Any], path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(out)
    return out


def load_state(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"phase state must be an object: {path}")
    return data


def validate_traffic_state(
    state: dict[str, Any],
    *,
    expected_invocation_id: str | None = None,
    min_submitted_per_pattern: int = 5,
) -> list[PatternReport]:
    """Reject stale / forged / wrong-context state. Returns pattern reports."""
    if int(state.get("state_version") or 0) != STATE_VERSION:
        raise ValueError(f"unsupported state_version: {state.get('state_version')!r}")
    inv = str(state.get("invocation_id") or "")
    expect = expected_invocation_id or os.environ.get(ENV_INVOCATION_ID, "").strip()
    if expect and inv != expect:
        raise ValueError(f"stale phase state: invocation_id={inv!r} != expected {expect!r}")
    sha = str(state.get("source_sha") or "")
    head = git_head_sha()
    if sha != head:
        raise ValueError(f"phase state source_sha={sha!r} != git HEAD={head!r}")
    policy = str(state.get("residency_policy") or "")
    live = residency_policy()
    if policy != live:
        raise ValueError(f"phase state policy={policy!r} != live {live!r}")
    obs = state.get("observations") or {}
    reports = deserialize_pattern_reports(list(obs.get("pattern_reports") or []))
    if not reports:
        raise ValueError("phase state has no pattern_reports")
    for report in reports:
        submitted = len(report.results)
        completed = sum(1 for r in report.results if r.ok)
        if submitted < min_submitted_per_pattern:
            raise ValueError(f"pattern {report.pattern!r} submitted={submitted} < {min_submitted_per_pattern}")
        if completed != submitted:
            raise ValueError(f"pattern {report.pattern!r} incomplete: completed={completed} submitted={submitted}")
    if int(obs.get("cold_load") or 0) < 1:
        raise ValueError("phase state cold_load < 1")
    return reports


def lifecycle_prepared() -> bool:
    return os.environ.get(ENV_LIFECYCLE_PREPARED, "").lower() in {"1", "true", "yes"}


def require_lifecycle_prepared() -> None:
    if not lifecycle_prepared():
        raise RuntimeError(
            "lifecycle phase requires external prepare: "
            "python -m harness.stack.run_phased_benchmark "
            "(or bootstrap_benchmark --prepare-lifecycle with "
            f"{ENV_LIFECYCLE_PREPARED}=1). Tests must not restart compose."
        )
