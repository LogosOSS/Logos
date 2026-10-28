"""Unit tests for PR comment posting (mocked GitHub API, no network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

import pytest

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))

import pr_comment  # noqa: E402
from report import _ANCHOR  # noqa: E402

SHA = "b" * 40


def _valid(policy: str) -> dict:
    return {
        "schema_version": 1,
        "source_sha": SHA,
        "residency_policy": policy,
        "synthetic": True,
        "scientific_claims": False,
        "patterns": [
            {
                "pattern": name,
                "submitted": 8,
                "completed": 8,
                "failures": 0,
                "missing_models": [],
            }
            for name in ("burst", "poisson", "sequential", "mixed")
        ],
        "lifecycle": {"cold_load": 3, "sleep": 1, "wake": 1},
        "vram": {"observation_count": 12, "violations": 0},
        "functional_verdict": "PASS",
    }


def _seed_reports(root: Path) -> None:
    for policy in ("demand", "lru"):
        d = root / policy
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(_valid(policy)), encoding="utf-8")
        (d / "junit.xml").write_text(
            '<?xml version="1.0"?><testsuite tests="2" failures="0" errors="0" skipped="0"/>',
            encoding="utf-8",
        )


def test_check_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_reports(tmp_path)
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    assert pr_comment.main(["--check"]) == 0


def test_check_fail_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    assert pr_comment.main(["--check"]) == 1


def test_check_single_policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_valid("demand")), encoding="utf-8")
    (tmp_path / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuite tests="1" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    monkeypatch.setenv("E2E_RESIDENCY_POLICY", "demand")
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    assert pr_comment.main(["--check", "--policy-file", str(path)]) == 0


def test_dry_run_prints_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    _seed_reports(tmp_path)
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("LOGOS_BENCH_PR_COMMENT_DRY_RUN", "1")
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    code = pr_comment.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert _ANCHOR in out
    assert SHA in out
    assert "residency simulation" in out.lower()
    assert "✅ PASS" in out


def test_idempotent_update_patches_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_reports(tmp_path)
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("LOGOS_BENCH_PR_NUMBER", "42")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.example.test")

    calls: List[Tuple[str, str, Any]] = []

    def fake_api(method: str, url: str, token: str, payload: Dict[str, Any] | None = None):
        calls.append((method, url, payload))
        if method == "GET":
            return [
                {
                    "id": 7,
                    "body": f"old\n{_ANCHOR}",
                    "user": {"login": "github-actions[bot]"},
                }
            ]
        return {}

    with mock.patch.object(pr_comment, "_api_request", side_effect=fake_api):
        code = pr_comment.main([])
    assert code == 0
    assert any(c[0] == "PATCH" and "/issues/comments/7" in c[1] for c in calls)
    patch_payload = next(c[2] for c in calls if c[0] == "PATCH")
    assert _ANCHOR in patch_payload["body"]
    assert "✅ PASS" in patch_payload["body"]


def test_create_when_no_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_reports(tmp_path)
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("LOGOS_BENCH_PR_NUMBER", "9")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.example.test")

    calls: List[Tuple[str, str, Any]] = []

    def fake_api(method: str, url: str, token: str, payload: Dict[str, Any] | None = None):
        calls.append((method, url, payload))
        if method == "GET":
            return []
        return {}

    with mock.patch.object(pr_comment, "_api_request", side_effect=fake_api):
        assert pr_comment.main([]) == 0
    assert any(c[0] == "POST" and c[2] and _ANCHOR in c[2]["body"] for c in calls)


def test_comment_api_failure_does_not_mask_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_reports(tmp_path)
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", SHA)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("LOGOS_BENCH_PR_NUMBER", "1")

    with mock.patch.object(pr_comment, "_api_request", side_effect=OSError("forbidden")):
        assert pr_comment.main([]) == 0


def test_failed_reports_still_attempt_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOGOS_SIM_REPORT_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "org/repo")
    monkeypatch.setenv("LOGOS_BENCH_PR_NUMBER", "3")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.example.test")

    posted: List[str] = []

    def fake_api(method: str, url: str, token: str, payload: Dict[str, Any] | None = None):
        if method == "GET":
            return []
        if method == "POST" and payload:
            posted.append(payload["body"])
        return {}

    with mock.patch.object(pr_comment, "_api_request", side_effect=fake_api):
        code = pr_comment.main([])
    assert code == 1
    assert posted and "FAIL" in posted[0] and _ANCHOR in posted[0]
    assert "NOTAVAILABLE" in posted[0]
