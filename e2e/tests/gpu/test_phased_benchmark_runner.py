"""Unit tests for phased-benchmark runner helpers (no Docker / no stack)."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from harness.stack.junit_merge import merge_junit_files
from harness.stack.phase_state import (
    STATE_VERSION,
    build_traffic_state,
    load_state,
    validate_traffic_state,
    write_state,
)
from harness.workload import PatternReport, RequestResult


def _ok_report(pattern: str, n: int = 5) -> PatternReport:
    results = [
        RequestResult(
            model=f"m{i % 5}",
            pattern=pattern,
            status_code=200,
            ok=True,
            completion_tokens=1,
            text="x",
        )
        for i in range(n)
    ]
    # ensure five distinct models for models_ok breadth is not required by validate
    return PatternReport(pattern=pattern, results=results)


def test_junit_merge_preserves_failures_from_both_phases(tmp_path: Path):
    a = tmp_path / "a.xml"
    b = tmp_path / "b.xml"
    a.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="t"><testsuite name="traffic" tests="2" failures="1" errors="0" skipped="0" time="1.0">
<testcase classname="t" name="ok" time="0.1"/>
<testcase classname="t" name="bad" time="0.2"><failure message="boom">x</failure></testcase>
</testsuite></testsuites>
""",
        encoding="utf-8",
    )
    b.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="lifecycle" tests="1" failures="0" errors="1" skipped="0" time="0.5">
<testcase classname="l" name="err" time="0.5"><error message="e">y</error></testcase>
</testsuite>
""",
        encoding="utf-8",
    )
    out = tmp_path / "merged.xml"
    merge_junit_files([a, b], dest=out)
    root = ET.parse(out).getroot()
    assert root.tag == "testsuites"
    assert int(root.get("tests") or 0) == 3
    assert int(root.get("failures") or 0) == 1
    assert int(root.get("errors") or 0) == 1
    names = {c.get("name") for s in root for c in s.findall("testcase")}
    assert names == {"ok", "bad", "err"}


def test_junit_merge_records_missing_artifact_as_error(tmp_path: Path):
    missing = tmp_path / "nope.xml"
    present = tmp_path / "ok.xml"
    present.write_text(
        '<testsuite name="only" tests="1" failures="0" errors="0" skipped="0" time="0">'
        '<testcase classname="c" name="t" time="0"/></testsuite>',
        encoding="utf-8",
    )
    out = tmp_path / "merged.xml"
    merge_junit_files([present, missing], dest=out)
    root = ET.parse(out).getroot()
    assert int(root.get("errors") or 0) >= 1
    assert any("missing" in (c.get("name") or "") for s in root for c in s.findall("testcase"))


def test_phase_state_rejects_stale_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("E2E_SIM_SOURCE_SHA", "")
    # Avoid SHA mismatch by patching git_head inside validate via env agreement:
    from harness.stack import phase_state as ps

    sha = "a" * 40
    monkeypatch.setattr(ps, "git_head_sha", lambda **_k: sha)
    monkeypatch.setattr(ps, "residency_policy", lambda: "demand")
    monkeypatch.setenv("E2E_RESIDENCY_POLICY", "demand")

    reports = [_ok_report("burst", 5), _ok_report("poisson", 5)]
    state = {
        "state_version": STATE_VERSION,
        "invocation_id": "aaa",
        "source_sha": sha,
        "residency_policy": "demand",
        "observations": {
            "pattern_reports": ps.serialize_pattern_reports(reports),
            "cold_load": 2,
            "vram_samples": 1,
            "vram_violations": 0,
        },
    }
    path = tmp_path / "state.json"
    write_state(state, path)
    loaded = load_state(path)
    with pytest.raises(ValueError, match="stale phase state"):
        validate_traffic_state(loaded, expected_invocation_id="bbb")


def test_phase_state_rejects_incomplete_pattern(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from harness.stack import phase_state as ps

    sha = "b" * 40
    monkeypatch.setattr(ps, "git_head_sha", lambda **_k: sha)
    monkeypatch.setattr(ps, "residency_policy", lambda: "lru")
    bad = PatternReport(
        pattern="burst",
        results=[
            RequestResult(model="m", pattern="burst", status_code=500, ok=False),
            RequestResult(model="m", pattern="burst", status_code=200, ok=True),
            RequestResult(model="m", pattern="burst", status_code=200, ok=True),
            RequestResult(model="m", pattern="burst", status_code=200, ok=True),
            RequestResult(model="m", pattern="burst", status_code=200, ok=True),
        ],
    )
    state = {
        "state_version": STATE_VERSION,
        "invocation_id": "inv",
        "source_sha": sha,
        "residency_policy": "lru",
        "observations": {
            "pattern_reports": ps.serialize_pattern_reports([bad]),
            "cold_load": 1,
            "vram_samples": 1,
            "vram_violations": 0,
        },
    }
    with pytest.raises(ValueError, match="incomplete"):
        validate_traffic_state(state, expected_invocation_id="inv")


def test_build_traffic_state_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from harness.stack import phase_state as ps

    sha = "c" * 40
    monkeypatch.setattr(ps, "git_head_sha", lambda **_k: sha)
    monkeypatch.setattr(ps, "residency_policy", lambda: "demand")
    reports = [_ok_report(p, 5) for p in ("burst", "poisson", "sequential", "mixed")]
    state = build_traffic_state(
        invocation_id="xyz",
        pattern_reports=reports,
        cold_load=3,
        vram_samples=10,
        vram_violations=0,
    )
    path = write_state(state, tmp_path / "s.json")
    loaded = load_state(path)
    out = validate_traffic_state(loaded, expected_invocation_id="xyz")
    assert [r.pattern for r in out] == ["burst", "poisson", "sequential", "mixed"]
    assert json.loads(path.read_text())["source_sha"] == sha


def test_archive_if_exists_preserves_prior_pass_json(tmp_path: Path):
    from harness.stack.run_phased_benchmark import _archive_if_exists

    report = tmp_path / "result.json"
    report.write_text('{"functional_verdict":"PASS"}\n', encoding="utf-8")
    archived = _archive_if_exists(report, invocation_id="abc123")
    assert archived is not None and archived.is_file()
    assert not report.exists()
    assert "PASS" in archived.read_text(encoding="utf-8")


def test_prepare_failure_junit_has_error_and_keeps_traffic(tmp_path: Path):
    from harness.stack.run_phased_benchmark import _write_runner_failure_junit

    traffic = tmp_path / "junit-traffic.xml"
    traffic.write_text(
        '<testsuite name="traffic" tests="1" failures="0" errors="0" skipped="0" time="1">'
        '<testcase classname="t" name="ok" time="1"/></testsuite>',
        encoding="utf-8",
    )
    out = tmp_path / "junit.xml"
    _write_runner_failure_junit(
        out,
        name="prepare_lifecycle",
        message="external lifecycle prepare failed",
        detail="boom",
        prior_phase_junits=[traffic],
    )
    root = ET.parse(out).getroot()
    assert int(root.get("errors") or 0) >= 1
    names = {c.get("name") for s in root for c in s.findall("testcase")}
    assert "ok" in names
    assert "prepare_lifecycle" in names


@pytest.mark.parametrize("awake_first", [True, False])
def test_lifecycle_setup_retains_awake_target_and_removes_other_replica(awake_first):
    from harness.stack.bootstrap_benchmark import retain_single_model_lane

    awake = {"model": "target", "lane_id": "target-2", "sleep_state": "awake", "pid": 703}
    sleeping = {"model": "target", "lane_id": "target-1", "sleep_state": "sleeping", "pid": 467}
    other = {"model": "pressure", "lane_id": "pressure-1", "sleep_state": "awake"}

    class Admin:
        def __init__(self):
            self.lanes = ([awake, sleeping] if awake_first else [sleeping, awake]) + [other]

        def node_lanes(self, provider_id):
            assert provider_id == 1
            return self.lanes

        def delete_lane(self, provider_id, lane_id):
            assert provider_id == 1
            self.lanes = [lane for lane in self.lanes if lane["lane_id"] != lane_id]

    admin = Admin()
    assert retain_single_model_lane(admin, "target", 1) == [(1, "target-1")]
    assert admin.lanes == [awake, other]
    assert retain_single_model_lane(admin, "target", 1) == []


def test_lifecycle_setup_does_not_accept_unconfirmed_replica_deletion(monkeypatch):
    from harness.stack import bootstrap_benchmark as bootstrap

    class Admin:
        def node_lanes(self, provider_id):
            return [
                {"model": "target", "lane_id": "one", "sleep_state": "awake"},
                {"model": "target", "lane_id": "two", "sleep_state": "sleeping"},
            ]

        def delete_lane(self, provider_id, lane_id):
            pass

    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    with pytest.raises(TimeoutError, match="single-target setup incomplete"):
        bootstrap.retain_single_model_lane(Admin(), "target", 1, timeout_s=1.0)
