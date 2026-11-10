"""Workflow YAML contract checks for the residency simulation CI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _ROOT / ".github" / "workflows" / "logos_benchmark-simulation.yml"
_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))

from report import NA, build_markdown, gate_policy_report, load_aggregate  # noqa: E402


def _load_workflow() -> dict:
    """Parse Actions YAML; PyYAML 1.1 maps the key ``on`` to boolean ``True``."""
    assert _WORKFLOW.is_file(), f"missing workflow: {_WORKFLOW}"
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


@pytest.fixture(scope="module")
def workflow() -> dict:
    return _load_workflow()


def test_workflow_yaml_parses(workflow: dict):
    assert workflow["name"]
    assert "pull_request" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    # Expressions used for head identity must appear literally in the file.
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.pull_request.head.sha" in text
    assert "E2E_SIM_SOURCE_SHA" in text


def test_simulate_checks_out_pr_head_not_default_merge(workflow: dict):
    sim = workflow["jobs"]["simulate"]
    checkout = next(s for s in sim["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    assert "head.repo.full_name" in checkout["with"].get("repository", "")
    env = sim["env"]
    assert env["E2E_SIM_SOURCE_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert env["LOGOS_SIM_SOURCE_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_aggregate_also_checks_out_pr_head(workflow: dict):
    agg = workflow["jobs"]["aggregate"]
    assert agg["if"] == "always()"
    assert agg["permissions"] == {"contents": "read"}
    checkout = next(s for s in agg["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    render = next(s for s in agg["steps"] if s.get("id") == "render")
    assert render["env"]["E2E_SIM_SOURCE_SHA"] == "${{ github.event.pull_request.head.sha }}"


def test_no_junit_gpu_fallback_into_integrated_junit():
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "junit-gpu.xml" in text  # gpu suite still recorded separately
    assert "do NOT copy junit-gpu.xml" in text or "Intentionally do NOT copy junit-gpu" in text
    for line in text.splitlines():
        stripped = line.strip()
        if "junit-gpu" in stripped and "junit.xml" in stripped:
            assert not stripped.startswith("cp ")
            assert "copy" not in stripped.lower() or "NOT copy" in stripped or "do NOT" in stripped


def test_download_artifacts_tolerant(workflow: dict):
    agg = workflow["jobs"]["aggregate"]
    download = next(s for s in agg["steps"] if s.get("id") == "download")
    assert download.get("continue-on-error") is True
    assert download["uses"].startswith("actions/download-artifact@")


def test_write_post_job_has_no_checkout_or_pr_python(workflow: dict):
    post = workflow["jobs"]["post-comment"]
    assert post["if"] == "always()"
    assert post["needs"] == "aggregate" or post["needs"] == ["aggregate"]
    perms = post["permissions"]
    assert perms.get("pull-requests") == "write"
    assert "contents" not in perms or perms.get("contents") != "write"
    steps = post["steps"]
    assert not any(s.get("uses", "").startswith("actions/checkout@") for s in steps)
    assert not any("pr_comment.py" in str(s.get("run", "")) or "python " in str(s.get("run", "")) for s in steps)
    script_step = next(s for s in steps if s.get("uses", "").startswith("actions/github-script@"))
    assert script_step.get("continue-on-error") is True
    script = script_step["with"]["script"]
    assert "logos-residency-simulation-bench" in script
    assert "fallback" in script
    assert "github-actions[bot]" in script


def test_no_pull_request_target(workflow: dict):
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request_target" not in text
    assert "workflow_run" not in yaml.dump(workflow.get("on", {}))


def test_fail_closed_missing_integrated_junit_even_with_gpu_junit(tmp_path: Path):
    sha = "d" * 40
    policy_dir = tmp_path / "demand"
    policy_dir.mkdir()
    result = {
        "schema_version": 1,
        "source_sha": sha,
        "residency_policy": "demand",
        "synthetic": True,
        "scientific_claims": False,
        "patterns": [
            {
                "pattern": n,
                "submitted": 8,
                "completed": 8,
                "failures": 0,
                "missing_models": [],
            }
            for n in ("burst", "poisson", "sequential", "mixed")
        ],
        "lifecycle": {"cold_load": 2, "sleep": 1, "wake": 1},
        "vram": {"observation_count": 10, "violations": 0},
        "functional_verdict": "PASS",
    }
    (policy_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (policy_dir / "junit-gpu.xml").write_text(
        '<?xml version="1.0"?><testsuite tests="5" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    # No junit.xml — integrated suite missing.
    data = json.loads((policy_dir / "result.json").read_text(encoding="utf-8"))
    verdict, reasons = gate_policy_report(data, None, expected_policy="demand", expected_sha=sha)
    assert verdict == "FAIL"
    assert any("junit required" in r for r in reasons)

    reports, junit, reasons = load_aggregate(tmp_path)
    assert reports["demand"] is not None
    assert junit["demand"] is None  # gpu junit must not be auto-loaded as integrated
    assert reasons["demand"]  # gate fail reasons present


def test_missing_artifacts_render_red_notavailable(tmp_path: Path):
    reports, junit, reasons = load_aggregate(tmp_path)
    md = build_markdown(
        reports,
        policy_reasons=reasons,
        junit_by_policy=junit,
        source_sha="e" * 40,
        overall="FAIL",
    )
    assert "❌ FAIL" in md
    assert NA in md
    assert f"| `demand` | `burst` | {NA} |" in md
    # Distinguish NA from a literal zero in missing rows.
    assert "| `demand` | `burst` | 0 |" not in md


def test_e2e_sim_source_sha_preferred_over_github_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    head = "f" * 40
    merge = "0" * 40
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", head)
    monkeypatch.setenv("GITHUB_SHA", merge)
    monkeypatch.delenv("LOGOS_SIM_SOURCE_SHA", raising=False)
    from report import expected_checkout_sha

    assert expected_checkout_sha() == head
    data = {
        "schema_version": 1,
        "source_sha": head,
        "residency_policy": "demand",
        "synthetic": True,
        "scientific_claims": False,
        "patterns": [
            {
                "pattern": n,
                "submitted": 5,
                "completed": 5,
                "failures": 0,
                "missing_models": [],
            }
            for n in ("burst", "poisson", "sequential", "mixed")
        ],
        "lifecycle": {"cold_load": 1, "sleep": 1, "wake": 1},
        "vram": {"observation_count": 1, "violations": 0},
        "functional_verdict": "PASS",
    }
    junit = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    verdict, reasons = gate_policy_report(data, junit, expected_policy="demand")
    assert verdict == "PASS", reasons
    # Wrong SHA (merge) must fail when expected is head.
    data["source_sha"] = merge
    verdict, reasons = gate_policy_report(data, junit, expected_policy="demand")
    assert verdict == "FAIL"
    assert any("disagrees with checkout" in r for r in reasons)
