"""Unit tests for residency simulation report parsing and gating."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))

from report import (  # noqa: E402
    NA,
    build_markdown,
    gate_policy_report,
    load_aggregate,
    load_json,
    parse_junit,
    validate_policy_report,
)

SHA = "a" * 40


def _valid(policy: str = "demand", *, sha: str = SHA) -> dict:
    return {
        "schema_version": 1,
        "source_sha": sha,
        "residency_policy": policy,
        "synthetic": True,
        "scientific_claims": False,
        "patterns": [
            {
                "pattern": name,
                "submitted": 20,
                "completed": 20,
                "failures": 0,
                "missing_models": [],
            }
            for name in ("burst", "poisson", "sequential", "mixed")
        ],
        "per_model_failures": {},
        "lifecycle": {"cold_load": 5, "sleep": 1, "wake": 1},
        "vram": {"observation_count": 40, "violations": 0},
        "functional_verdict": "PASS",
    }


def _junit_xml(tmp_path: Path, *, failures: int = 0, tests: int = 3) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        f'<?xml version="1.0"?><testsuite tests="{tests}" failures="{failures}" ' f'errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    return path


def test_valid_report_gates_pass(tmp_path: Path):
    junit = parse_junit(_junit_xml(tmp_path))
    verdict, reasons = gate_policy_report(_valid(), junit, expected_policy="demand")
    assert verdict == "PASS"
    assert reasons == []


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(Exception) as exc:
        load_json(tmp_path / "missing.json")
    assert "not found" in str(exc.value)


def test_invalid_json(tmp_path: Path):
    path = tmp_path / "result.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(Exception) as exc:
        load_json(path)
    assert "invalid JSON" in str(exc.value)


def test_invalid_schema_version():
    data = _valid()
    data["schema_version"] = 99
    reasons = validate_policy_report(data)
    assert any("schema_version" in r for r in reasons)


def test_source_sha_must_be_40_hex():
    data = _valid()
    data["source_sha"] = "deadbeef"
    reasons = validate_policy_report(data)
    assert any("source_sha" in r for r in reasons)


def test_partial_patterns_fail():
    data = _valid()
    data["patterns"] = data["patterns"][:2]
    reasons = validate_policy_report(data)
    assert any("missing patterns" in r for r in reasons)


def test_one_populated_three_empty_fail():
    data = _valid()
    for entry in data["patterns"][1:]:
        entry["submitted"] = 0
        entry["completed"] = 0
    reasons = validate_policy_report(data)
    assert any("submitted=" in r and "< 5" in r for r in reasons)


def test_submitted_lt_five_fail():
    data = _valid()
    data["patterns"][0]["submitted"] = 4
    data["patterns"][0]["completed"] = 4
    reasons = validate_policy_report(data)
    assert any("submitted=4" in r for r in reasons)


def test_completed_must_equal_submitted():
    data = _valid()
    data["patterns"][0]["completed"] = 19
    reasons = validate_policy_report(data)
    assert any("completed" in r and "!=" in r for r in reasons)


def test_bool_counter_rejected():
    data = _valid()
    data["patterns"][0]["submitted"] = True  # type: ignore[assignment]
    reasons = validate_policy_report(data)
    assert any("must be an int" in r for r in reasons)


def test_lifecycle_zero_not_exercised():
    data = _valid()
    data["lifecycle"]["sleep"] = 0
    reasons = validate_policy_report(data)
    assert any("lifecycle.sleep" in r for r in reasons)


def test_zero_vram_observations_fail_closed():
    data = _valid()
    data["vram"]["observation_count"] = 0
    verdict, reasons = gate_policy_report(data, {"tests": 1, "failures": 0, "errors": 0, "skipped": 0})
    assert verdict == "FAIL"
    assert any("observation_count" in r for r in reasons)


def test_vram_violations_fail():
    data = _valid()
    data["vram"]["violations"] = 2
    verdict, reasons = gate_policy_report(data, {"tests": 1, "failures": 0, "errors": 0, "skipped": 0})
    assert verdict == "FAIL"
    assert any("violations" in r for r in reasons)


def test_missing_models_fail():
    data = _valid()
    data["patterns"][0]["missing_models"] = ["google/gemma-3-4b-it"]
    verdict, reasons = gate_policy_report(data, {"tests": 1, "failures": 0, "errors": 0, "skipped": 0})
    assert verdict == "FAIL"
    assert any("missing_models" in r for r in reasons)


def test_junit_required():
    verdict, reasons = gate_policy_report(_valid(), None)
    assert verdict == "FAIL"
    assert any("junit required" in r for r in reasons)


def test_failed_startup_aggregate_marks_na(tmp_path: Path):
    reports, junit, reasons = load_aggregate(tmp_path)
    assert reports["demand"] is None and reports["lru"] is None
    assert reasons["demand"] == ["report missing"]
    md = build_markdown(reports, policy_reasons=reasons, junit_by_policy=junit)
    assert NA in md
    assert "0 |" not in md.split("Lifecycle")[0] or NA in md  # pattern cells are NA
    assert f"| `demand` | `burst` | {NA} |" in md
    assert "FAIL" in md
    assert "<!-- logos-residency-simulation-bench -->" in md


def test_partial_data_one_policy(tmp_path: Path):
    demand = tmp_path / "demand"
    demand.mkdir()
    (demand / "result.json").write_text(json.dumps(_valid("demand")), encoding="utf-8")
    (demand / "junit.xml").write_text(
        '<?xml version="1.0"?><testsuite tests="2" failures="0" errors="0" skipped="0"/>',
        encoding="utf-8",
    )
    reports, junit, reasons = load_aggregate(tmp_path)
    assert reports["demand"] is not None
    assert reports["lru"] is None
    assert reasons["lru"] == ["report missing"]
    md = build_markdown(reports, policy_reasons=reasons, junit_by_policy=junit, overall="FAIL")
    assert "`lru`" in md and NA in md
    assert "❌ FAIL" in md


def test_cross_policy_sha_mismatch(tmp_path: Path):
    for policy, sha in (("demand", "a" * 40), ("lru", "b" * 40)):
        d = tmp_path / policy
        d.mkdir()
        (d / "result.json").write_text(json.dumps(_valid(policy, sha=sha)), encoding="utf-8")
        (d / "junit.xml").write_text(
            '<?xml version="1.0"?><testsuite tests="2" failures="0" errors="0" skipped="0"/>',
            encoding="utf-8",
        )
    _reports, _junit, reasons = load_aggregate(tmp_path)
    assert any("disagrees across policies" in r for rs in reasons.values() for r in rs)


def test_junit_failures_fail_gate(tmp_path: Path):
    junit = parse_junit(_junit_xml(tmp_path, failures=1))
    verdict, reasons = gate_policy_report(_valid(), junit)
    assert verdict == "FAIL"
    assert any("junit failures" in r for r in reasons)


def test_sha_html_escaped_in_markdown():
    "a" * 39 + "<"
    # invalid sha fails gate, but display path escapes when forced
    reports = {"demand": None, "lru": None}
    md = build_markdown(reports, source_sha="abc<script>x</script>")
    assert "<script>" not in md
    assert "&lt;script&gt;" in md


def test_markdown_tables_include_lifecycle(tmp_path: Path):
    junit = {"tests": 2, "failures": 0, "errors": 0, "skipped": 0}
    reports = {policy: _valid(policy) for policy in ("demand", "lru", "simple")}
    md = build_markdown(
        reports,
        junit_by_policy={policy: junit for policy in ("demand", "lru", "simple")},
        source_sha=SHA,
        run_url="https://example.test/run/1",
    )
    assert "Cold load" in md
    assert "`demand`" in md and "`lru`" in md
    assert "`burst`" in md
    assert SHA in md
    assert "workflow run" in md
    assert "not GPU performance" in md
    assert "0 |" in md  # real zeros (e.g. failures column) distinct from NA rows


def _performance(policy="demand"):
    return {
        "synthetic": True,
        "source_sha": SHA,
        "time_factor": 10,
        "matrix_complete": True,
        "cases": [
            {
                "policy": policy,
                "pattern": pattern,
                "seed": 42,
                "workload_sha256": "a" * 64,
                "summary": {
                    "submitted": 25,
                    "completed": 25,
                    "failures": 0,
                    "vram_observations": 10,
                    "vram_violations": 0,
                    **{
                        f"{metric}_{q}": value
                        for metric in ("ttft_s", "ttlt_s", "time_to_outcome_s")
                        for q, value in (("p50", 1), ("p95", 2), ("p99", 3))
                    },
                },
            }
            for pattern in ("burst", "poisson", "sequential", "mixed")
        ],
    }


def test_performance_gate_rejects_partial_matrix_and_missing_latencies():
    from report import validate_performance

    perf = _performance()
    assert validate_performance(perf, "demand", SHA) == []
    perf["matrix_complete"] = False
    assert validate_performance(perf, "demand", SHA)
    perf["matrix_complete"] = True
    del perf["cases"][0]["summary"]["ttft_s_p99"]
    assert validate_performance(perf, "demand", SHA)


def test_required_performance_cannot_be_replaced_by_functional_report(monkeypatch):
    monkeypatch.setenv("LOGOS_SIM_REQUIRE_PERFORMANCE", "1")
    verdict, reasons = gate_policy_report(_valid(), {"tests": 1, "failures": 0, "errors": 0})
    assert verdict == "FAIL" and any("performance" in reason for reason in reasons)


def test_comment_includes_simulated_latency_comparison():
    reports = {
        policy: {**_valid(policy), "performance": _performance(policy)} for policy in ("demand", "lru", "simple")
    }
    junit = {policy: {"tests": 1, "failures": 0, "errors": 0} for policy in reports}
    text = build_markdown(reports, junit_by_policy=junit, policy_reasons={})
    assert "TTFT p50/p95/p99" in text
    assert "No GPU performance or energy claim" in text
    assert "`simple`" in text
