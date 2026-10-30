"""Parse residency-simulation JSON (+ JUnit) and render PR markdown.

Facts only from the files on disk. Missing or invalid data becomes
``NOTAVAILABLE`` / gate FAIL — never invent zeros. Stdlib only.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
REQUIRED_POLICIES: Tuple[str, ...] = ("demand", "lru", "simple")
REQUIRED_PATTERNS: Tuple[str, ...] = ("burst", "poisson", "sequential", "mixed")
MIN_SUBMITTED_PER_PATTERN = 5
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
NA = "NOTAVAILABLE"

_ANCHOR = "<!-- logos-residency-simulation-bench -->"


class ReportError(ValueError):
    """Invalid or incomplete simulation report."""


def load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ReportError(f"simulation result not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReportError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportError(f"simulation result root must be an object: {path}")
    return data


def parse_junit(path: Path) -> Dict[str, Any]:
    """Return ``{tests, failures, errors, skipped}`` or raise ``ReportError``."""
    if not path.is_file():
        raise ReportError(f"junit not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ReportError(f"invalid junit XML in {path}: {exc}") from exc

    if root.tag == "testsuites":
        tests = failures = errors = skipped = 0
        for suite in root.findall("testsuite"):
            tests += int(float(suite.attrib.get("tests", 0)))
            failures += int(float(suite.attrib.get("failures", 0)))
            errors += int(float(suite.attrib.get("errors", 0)))
            skipped += int(float(suite.attrib.get("skipped", 0)))
        return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped}
    return {
        "tests": int(float(root.attrib.get("tests", 0))),
        "failures": int(float(root.attrib.get("failures", 0))),
        "errors": int(float(root.attrib.get("errors", 0))),
        "skipped": int(float(root.attrib.get("skipped", 0))),
    }


def _int_field(obj: Dict[str, Any], key: str, *, required: bool = True) -> Optional[int]:
    if key not in obj:
        if required:
            raise ReportError(f"missing required integer field: {key}")
        return None
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError(f"field {key!r} must be an int, got {type(value).__name__}")
    if value < 0:
        raise ReportError(f"field {key!r} must be >= 0, got {value}")
    return value


def expected_checkout_sha() -> str:
    """PR-head identity expected by the gate.

    Prefer ``E2E_SIM_SOURCE_SHA`` (workflow sets this to
    ``pull_request.head.sha``). Do **not** treat the ephemeral merge-ref
    ``GITHUB_SHA`` as authoritative when an explicit head SHA is provided.
    """
    return (os.environ.get("E2E_SIM_SOURCE_SHA") or os.environ.get("LOGOS_SIM_SOURCE_SHA") or "").strip()


def validate_policy_report(
    data: Dict[str, Any],
    *,
    expected_policy: Optional[str] = None,
    expected_sha: Optional[str] = None,
) -> List[str]:
    """Return a list of gate failure reasons (empty ⇒ structurally OK so far)."""
    reasons: List[str] = []
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        reasons.append(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")
        return reasons

    if data.get("synthetic") is not True:
        reasons.append("synthetic must be true")
    if data.get("scientific_claims") is not False:
        reasons.append("scientific_claims must be false")

    sha = data.get("source_sha")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        reasons.append("source_sha must be exactly 40 hex characters")
    else:
        want = expected_sha if expected_sha is not None else expected_checkout_sha()
        if want and SHA_RE.fullmatch(want) and sha.lower() != want.lower():
            reasons.append(f"source_sha {sha} disagrees with checkout {want}")

    policy = data.get("residency_policy")
    if policy not in REQUIRED_POLICIES:
        reasons.append(f"residency_policy must be one of {REQUIRED_POLICIES}, got {policy!r}")
    elif expected_policy is not None and policy != expected_policy:
        reasons.append(f"residency_policy {policy!r} != expected matrix leg {expected_policy!r}")

    patterns = data.get("patterns")
    if not isinstance(patterns, list):
        reasons.append("patterns must be a list")
        return reasons

    seen: set[str] = set()
    for entry in patterns:
        if not isinstance(entry, dict):
            reasons.append("each pattern entry must be an object")
            continue
        name = entry.get("pattern")
        if name not in REQUIRED_PATTERNS:
            reasons.append(f"unknown pattern {name!r}")
            continue
        if name in seen:
            reasons.append(f"duplicate pattern {name!r}")
        seen.add(name)
        try:
            submitted = _int_field(entry, "submitted")
            completed = _int_field(entry, "completed")
            failures = _int_field(entry, "failures")
        except ReportError as exc:
            reasons.append(f"pattern {name}: {exc}")
            continue
        assert submitted is not None and completed is not None and failures is not None
        if submitted < MIN_SUBMITTED_PER_PATTERN:
            reasons.append(f"pattern {name}: submitted={submitted} < {MIN_SUBMITTED_PER_PATTERN}")
        if completed != submitted:
            reasons.append(f"pattern {name}: completed ({completed}) != submitted ({submitted})")
        if failures != 0:
            reasons.append(f"pattern {name}: failures={failures}")
        missing = entry.get("missing_models")
        if missing != []:
            reasons.append(f"pattern {name}: missing_models must be [] (got {missing!r})")

    missing_patterns = [p for p in REQUIRED_PATTERNS if p not in seen]
    if missing_patterns:
        reasons.append(f"missing patterns: {missing_patterns}")
    if len(seen) != len(REQUIRED_PATTERNS):
        reasons.append("must have exactly four distinct required patterns")

    lifecycle = data.get("lifecycle")
    if not isinstance(lifecycle, dict):
        reasons.append("lifecycle object missing")
    else:
        for key in ("cold_load", "sleep", "wake"):
            try:
                value = _int_field(lifecycle, key)
            except ReportError as exc:
                reasons.append(f"lifecycle.{exc}")
                continue
            assert value is not None
            if value < 1:
                reasons.append(f"lifecycle.{key} must be >= 1 (exercised), got {value}")

    vram = data.get("vram")
    if not isinstance(vram, dict):
        reasons.append("vram object missing")
    else:
        try:
            observations = _int_field(vram, "observation_count")
            violations = _int_field(vram, "violations")
        except ReportError as exc:
            reasons.append(f"vram.{exc}")
        else:
            assert observations is not None and violations is not None
            if observations < 1:
                reasons.append("vram.observation_count must be >= 1")
            if violations != 0:
                reasons.append(f"vram.violations must be 0, got {violations}")

    verdict = data.get("functional_verdict")
    if verdict not in ("PASS", "FAIL"):
        reasons.append(f"functional_verdict must be PASS or FAIL, got {verdict!r}")
    elif verdict != "PASS":
        reasons.append("functional_verdict is FAIL")

    return reasons


def validate_performance(data: Any, policy: str, sha: str) -> List[str]:
    """Reject absent, incomplete or inconsistent short synthetic measurements."""
    if not isinstance(data, dict):
        return ["performance report missing"]
    reasons = []
    if data.get("synthetic") is not True or data.get("source_sha") != sha:
        reasons.append("performance identity mismatch")
    speed = data.get("time_factor")
    if not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
        reasons.append("invalid performance time factor")
    cases = data.get("cases") or []
    if data.get("matrix_complete") is not True:
        reasons.append("performance matrix incomplete")
    if {case.get("pattern") for case in cases} != set(REQUIRED_PATTERNS):
        reasons.append("performance patterns incomplete")
    seen = set()
    for case in cases:
        key = (case.get("pattern"), case.get("seed"))
        if key in seen:
            reasons.append("duplicate performance case")
        seen.add(key)
        if case.get("policy") != policy:
            reasons.append("performance policy mismatch")
        if not re.fullmatch(r"[0-9a-f]{64}", str(case.get("workload_sha256", ""))):
            reasons.append("performance workload digest missing")
        summary = case.get("summary") or {}
        if (
            summary.get("submitted", 0) < 5
            or summary.get("completed") != summary.get("submitted")
            or summary.get("failures") != 0
        ):
            reasons.append("performance requests incomplete")
        if summary.get("vram_violations") != 0 or summary.get("vram_observations", 0) < 1:
            reasons.append("performance VRAM evidence invalid")
        for metric in ("ttft_s", "ttlt_s", "time_to_outcome_s"):
            values = [summary.get(f"{metric}_{q}") for q in ("p50", "p95", "p99")]
            if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
                reasons.append("performance latency missing or invalid")
            elif values != sorted(values):
                reasons.append("performance latency quantiles unordered")
    return reasons


def gate_policy_report(
    data: Dict[str, Any],
    junit: Optional[Dict[str, Any]] = None,
    *,
    expected_policy: Optional[str] = None,
    expected_sha: Optional[str] = None,
    require_junit: bool = True,
) -> Tuple[str, List[str]]:
    """Return ``(PASS|FAIL, reasons)``."""
    reasons = validate_policy_report(data, expected_policy=expected_policy, expected_sha=expected_sha)
    if "performance" in data or os.environ.get("LOGOS_SIM_REQUIRE_PERFORMANCE") == "1":
        reasons.extend(
            validate_performance(data.get("performance"), data.get("residency_policy"), data.get("source_sha"))
        )
    if require_junit and junit is None:
        reasons.append("junit required but missing")
    if junit is not None:
        if junit.get("tests", 0) <= 0:
            reasons.append("junit reports zero tests")
        if junit.get("failures", 0) or junit.get("errors", 0):
            reasons.append(f"junit failures={junit.get('failures', 0)} errors={junit.get('errors', 0)}")
    verdict = "PASS" if not reasons else "FAIL"
    return verdict, reasons


def _fmt(value: Any) -> str:
    if value is None:
        return NA
    return str(value)


def _cell_int(obj: Optional[Dict[str, Any]], *keys: str) -> str:
    cur: Any = obj
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return NA
        cur = cur[key]
    if isinstance(cur, bool) or not isinstance(cur, int):
        return NA
    return str(cur)


def build_markdown(
    policy_reports: Dict[str, Optional[Dict[str, Any]]],
    *,
    policy_reasons: Optional[Dict[str, List[str]]] = None,
    junit_by_policy: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
    source_sha: str = "",
    run_url: str = "",
    overall: Optional[str] = None,
) -> str:
    """Render the aggregated PR comment body (includes HTML anchor)."""
    policy_reasons = policy_reasons or {}
    junit_by_policy = junit_by_policy or {}

    computed_overall = "PASS"
    for policy in REQUIRED_POLICIES:
        data = policy_reports.get(policy)
        if data is None:
            computed_overall = "FAIL"
            continue
        verdict, _ = gate_policy_report(data, junit_by_policy.get(policy), expected_policy=policy)
        if verdict != "PASS":
            computed_overall = "FAIL"
    if overall is None:
        overall = computed_overall

    icon = {"PASS": "✅", "FAIL": "❌"}.get(overall, "❌")
    sha_raw = source_sha.strip()
    if not sha_raw:
        # Prefer a SHA from any present report for display.
        for policy in REQUIRED_POLICIES:
            data = policy_reports.get(policy) or {}
            candidate = data.get("source_sha")
            if isinstance(candidate, str) and candidate:
                sha_raw = candidate
                break
    sha_display = html.escape(sha_raw) if sha_raw else NA
    run_display = f"[workflow run]({run_url})" if run_url else NA

    lines: List[str] = [
        f"## {icon} {overall}: GPU-less residency simulation",
        "",
        "GPU-simulated functional exercise of residency policies against the "
        "real orchestrator and worker with a fake vLLM below the worker. "
        "**Times are not GPU performance.** Synthetic only; no scientific claims.",
        "",
        f"**SHA:** `{sha_display}` · **Run:** {run_display}",
        "",
        "### Per policy / pattern",
        "",
        "| Policy | Pattern | Submitted | Completed | Failures | Missing models |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]

    for policy in REQUIRED_POLICIES:
        data = policy_reports.get(policy)
        if data is None:
            for pattern in REQUIRED_PATTERNS:
                lines.append(f"| `{policy}` | `{pattern}` | {NA} | {NA} | {NA} | {NA} |")
            continue
        patterns = {p.get("pattern"): p for p in data.get("patterns", []) if isinstance(p, dict)}
        for pattern in REQUIRED_PATTERNS:
            entry = patterns.get(pattern)
            if not entry:
                lines.append(f"| `{policy}` | `{pattern}` | {NA} | {NA} | {NA} | {NA} |")
                continue
            missing = entry.get("missing_models")
            if isinstance(missing, list):
                missing_cell = str(len(missing))
            else:
                missing_cell = NA
            lines.append(
                f"| `{policy}` | `{pattern}` | {_fmt(entry.get('submitted'))} | "
                f"{_fmt(entry.get('completed'))} | {_fmt(entry.get('failures'))} | "
                f"{missing_cell} |"
            )

    lines += [
        "",
        "### Lifecycle & VRAM",
        "",
        "| Policy | Cold load | Sleep | Wake | VRAM observations | VRAM violations | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for policy in REQUIRED_POLICIES:
        data = policy_reports.get(policy)
        if data is None:
            lines.append(f"| `{policy}` | {NA} | {NA} | {NA} | {NA} | {NA} | FAIL |")
            continue
        junit = junit_by_policy.get(policy)
        verdict, _ = gate_policy_report(data, junit, expected_policy=policy)
        lines.append(
            f"| `{policy}` | {_cell_int(data, 'lifecycle', 'cold_load')} | "
            f"{_cell_int(data, 'lifecycle', 'sleep')} | "
            f"{_cell_int(data, 'lifecycle', 'wake')} | "
            f"{_cell_int(data, 'vram', 'observation_count')} | "
            f"{_cell_int(data, 'vram', 'violations')} | {verdict} |"
        )

    if any((policy_reports.get(policy) or {}).get("performance") for policy in REQUIRED_POLICIES):
        lines += [
            "",
            "### Synthetic performance (short CI run)",
            "",
            "Latencies are simulated seconds: observed wall time multiplied by the reported factor. "
            "Prefill and concurrency contain explicit estimates. No GPU performance or energy claim.",
            "",
            "| Policy | Pattern | Seed | Factor | Completion | TTFT p50/p95/p99 | TTLT p50/p95/p99 | "
            "Cold | Sleep | Wake | 507 | VRAM violations |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]

        def latency(summary, metric):
            return "/".join(
                f"{summary[f'{metric}_{q}']:.2f}" if isinstance(summary.get(f"{metric}_{q}"), (int, float)) else NA
                for q in ("p50", "p95", "p99")
            )

        for policy in REQUIRED_POLICIES:
            perf = (policy_reports.get(policy) or {}).get("performance") or {}
            for case in perf.get("cases", []):
                summary = case.get("summary") or {}
                lines.append(
                    f"| `{policy}` | `{case.get('pattern')}` | {case.get('seed')} | {perf.get('time_factor')} | "
                    f"{summary.get('completed')}/{summary.get('submitted')} | {latency(summary, 'ttft_s')} | "
                    f"{latency(summary, 'ttlt_s')} | {summary.get('cold_load', NA)} | {summary.get('sleep', NA)} | "
                    f"{summary.get('wake', NA)} | {summary.get('wake_rejected', NA)} | "
                    f"{summary.get('vram_violations', NA)} |"
                )

    detail_lines: List[str] = []
    for policy in REQUIRED_POLICIES:
        reasons = list(policy_reasons.get(policy) or [])
        data = policy_reports.get(policy)
        if data is None and not reasons:
            reasons = ["report missing"]
        elif data is not None and not reasons:
            _, reasons = gate_policy_report(data, junit_by_policy.get(policy), expected_policy=policy)
        if reasons:
            detail_lines.append(f"- `{policy}`: " + "; ".join(html.escape(r) for r in reasons))
    if detail_lines:
        lines += ["", "### Gate detail", ""] + detail_lines

    lines += ["", _ANCHOR]
    return "\n".join(lines)


def discover_policy_dirs(root: Path) -> Dict[str, Path]:
    """Map policy name → directory containing ``result.json`` under *root*."""
    found: Dict[str, Path] = {}
    if not root.exists():
        return found
    for policy in REQUIRED_POLICIES:
        direct = root / policy
        if (direct / "result.json").is_file():
            found[policy] = direct
            continue
        for candidate in root.rglob("result.json"):
            parent = candidate.parent
            name = parent.name
            if name == policy or name.endswith(f"-{policy}") or f"-{policy}-" in name:
                found[policy] = parent
                break
            try:
                data = load_json(candidate)
            except ReportError:
                continue
            if data.get("residency_policy") == policy and policy not in found:
                found[policy] = parent
    return found


def load_aggregate(root: Path) -> Tuple[
    Dict[str, Optional[Dict[str, Any]]],
    Dict[str, Optional[Dict[str, Any]]],
    Dict[str, List[str]],
]:
    """Load all policy reports + junit under *root*.

    Returns ``(reports, junit_by_policy, reasons)``.
    """
    dirs = discover_policy_dirs(root)
    reports: Dict[str, Optional[Dict[str, Any]]] = {}
    junit_by_policy: Dict[str, Optional[Dict[str, Any]]] = {}
    reasons: Dict[str, List[str]] = {}
    expected_sha = expected_checkout_sha()
    seen_shas: List[str] = []

    for policy in REQUIRED_POLICIES:
        directory = dirs.get(policy)
        if directory is None:
            reports[policy] = None
            junit_by_policy[policy] = None
            reasons[policy] = ["report missing"]
            continue
        try:
            data = load_json(directory / "result.json")
        except ReportError as exc:
            reports[policy] = None
            junit_by_policy[policy] = None
            reasons[policy] = [str(exc)]
            continue
        reports[policy] = data
        sha = data.get("source_sha")
        if isinstance(sha, str) and sha:
            seen_shas.append(sha)

        junit_path = directory / "junit.xml"
        junit: Optional[Dict[str, Any]] = None
        if junit_path.is_file():
            try:
                junit = parse_junit(junit_path)
            except ReportError as exc:
                reasons.setdefault(policy, []).append(str(exc))
        junit_by_policy[policy] = junit
        verdict, gate_reasons = gate_policy_report(
            data,
            junit,
            expected_policy=policy,
            expected_sha=expected_sha or None,
        )
        if verdict != "PASS":
            reasons[policy] = gate_reasons
        else:
            reasons.setdefault(policy, [])

    workloads = {}
    settings = set()
    for policy, data in reports.items():
        perf = (data or {}).get("performance") or {}
        if not perf:
            continue
        settings.add((perf.get("timing_profile_sha256"), perf.get("time_factor"), perf.get("rate_per_simulated_s")))
        for case in perf.get("cases", []):
            key = (case.get("pattern"), case.get("seed"))
            workloads.setdefault(key, set()).add(case.get("workload_sha256"))
    if len(settings) > 1 or any(len(values) > 1 for values in workloads.values()):
        for policy in REQUIRED_POLICIES:
            reasons.setdefault(policy, []).append("performance workloads or timing settings differ across policies")

    # Cross-leg SHA agreement.
    unique = {s.lower() for s in seen_shas}
    if len(unique) > 1:
        msg = f"source_sha disagrees across policies: {sorted(unique)}"
        for policy in REQUIRED_POLICIES:
            reasons.setdefault(policy, []).append(msg)

    return reports, junit_by_policy, reasons


def overall_verdict(
    reports: Dict[str, Optional[Dict[str, Any]]],
    junit_by_policy: Dict[str, Optional[Dict[str, Any]]],
) -> str:
    for policy in REQUIRED_POLICIES:
        data = reports.get(policy)
        if data is None:
            return "FAIL"
        verdict, _ = gate_policy_report(data, junit_by_policy.get(policy), expected_policy=policy)
        if verdict != "PASS":
            return "FAIL"
    # Cross-SHA already folded into reasons by load_aggregate; re-check here.
    shas = {
        (reports[p] or {}).get("source_sha", "").lower()  # type: ignore[union-attr]
        for p in REQUIRED_POLICIES
        if reports.get(p)
    }
    if len(shas) > 1:
        return "FAIL"
    return "PASS"
