"""Emit the GPU-less residency simulation observation report (schema v1).

Writes only observed facts to ``E2E_SIM_REPORT_PATH``. Never invents lifecycle
or VRAM counters. ``source_sha`` comes from ``git rev-parse HEAD``, optionally
cross-checked against ``E2E_SIM_SOURCE_SHA``. Canonical contract:
``benchmarks/residency_simulation/SCHEMA.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from harness.benchmark_topology import (
    BENCHMARK_MODELS_5,
    BENCHMARK_TRAFFIC_PATTERNS,
    TIMING_MODE,
    container_simulation_assumptions,
)
from harness.workload import PatternReport


def git_head_sha(*, repo: Path | None = None) -> str:
    root = repo or Path(__file__).resolve().parents[2]
    out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(root), text=True).strip()
    if len(out) != 40 or any(c not in "0123456789abcdefABCDEF" for c in out):
        raise RuntimeError(f"git rev-parse HEAD did not return a 40-hex SHA: {out!r}")
    env_sha = os.environ.get("E2E_SIM_SOURCE_SHA", "").strip()
    if env_sha and env_sha != out:
        raise RuntimeError(f"E2E_SIM_SOURCE_SHA={env_sha!r} disagrees with git HEAD={out!r}")
    return out


def residency_policy() -> str:
    return (os.environ.get("E2E_RESIDENCY_POLICY") or "demand").strip().lower() or "demand"


def build_report(
    *,
    pattern_reports: Sequence[PatternReport],
    models: Sequence[str] = BENCHMARK_MODELS_5,
    cold_load: int,
    sleep: int,
    wake: int,
    wake_same_pid: bool,
    vram_observation_count: int,
    vram_violations: int,
    lifecycle_trigger: str | None = None,
) -> dict[str, Any]:
    patterns_out: list[dict[str, Any]] = []
    per_model_failures = {m: 0 for m in models}
    for report in pattern_reports:
        submitted = len(report.results)
        completed = sum(1 for r in report.results if r.ok)
        failures = submitted - completed
        missing = [m for m in models if m not in report.models_ok()]
        for r in report.results:
            if not r.ok:
                per_model_failures[r.model] = per_model_failures.get(r.model, 0) + 1
        patterns_out.append(
            {
                "pattern": report.pattern,
                "submitted": submitted,
                "completed": completed,
                "failures": failures,
                "missing_models": missing,
            }
        )
    # Ensure all four pattern names present (fail-closed empty if absent).
    seen = {p["pattern"] for p in patterns_out}
    for name in BENCHMARK_TRAFFIC_PATTERNS:
        if name not in seen:
            patterns_out.append(
                {
                    "pattern": name,
                    "submitted": 0,
                    "completed": 0,
                    "failures": 0,
                    "missing_models": list(models),
                }
            )

    lifecycle = {"cold_load": int(cold_load), "sleep": int(sleep), "wake": int(wake)}
    # Same-PID wake is required for a real wake observation; without it wake=0.
    if wake > 0 and not wake_same_pid:
        lifecycle["wake"] = 0

    verdict = "PASS"
    if any(
        p["submitted"] < 5 or p["completed"] != p["submitted"] or p["failures"] != 0 or p["missing_models"]
        for p in patterns_out
    ):
        verdict = "FAIL"
    if lifecycle["cold_load"] < 1 or lifecycle["sleep"] < 1 or lifecycle["wake"] < 1:
        verdict = "FAIL"
    if int(vram_observation_count) < 1 or int(vram_violations) != 0:
        verdict = "FAIL"
    if not wake_same_pid:
        verdict = "FAIL"

    report: dict[str, Any] = {
        "schema_version": 1,
        "source_sha": git_head_sha(),
        "residency_policy": residency_policy(),
        "synthetic": True,
        "scientific_claims": False,
        "timing_mode": TIMING_MODE,  # accelerated_functional; not gated by reporter
        "patterns": patterns_out,
        "per_model_failures": per_model_failures,
        "lifecycle": lifecycle,
        "vram": {
            "observation_count": int(vram_observation_count),
            "violations": int(vram_violations),
        },
        "functional_verdict": verdict,
        "wake_same_pid": bool(wake_same_pid),
    }
    # Optional observation note (not gated): idle=60 stays configured; sleep may
    # be driven by capacity pressure when the planner retains the last lane.
    if lifecycle_trigger:
        report["lifecycle_trigger"] = str(lifecycle_trigger)
    # Prefer fixture/container wiring (what nodesim configures), not host-env fallback.
    assumptions = container_simulation_assumptions()
    try:
        from harness.gpusim.server import simulation_assumptions as gpusim_assumptions

        live = gpusim_assumptions()
        # Overlay only keys that prove the fake protocol version in this process.
        assumptions["fake_vllm_version"] = live.get("fake_vllm_version", assumptions.get("fake_vllm_version"))
        assumptions["fake_vllm_version_kind"] = live.get("fake_vllm_version_kind", "simulated_protocol")
    except Exception:
        pass
    report["simulation_assumptions"] = assumptions
    return report


def write_report(report: dict[str, Any], path: str | Path | None = None) -> Path | None:
    target = path or os.environ.get("E2E_SIM_REPORT_PATH", "").strip()
    if not target:
        return None
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return out
