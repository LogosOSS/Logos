"""External phased benchmark runner (compose lifecycle stays outside pytest).

Phases:
  1. pytest traffic/topology → write real observation state (on pass)
  2. ``prepare_lifecycle_isolation`` (restrict + lanes/delete + orch restart)
  3. pytest lifecycle/sleep-wake + schema report (loads state)
  4. merge JUnit XML from both phases (never drop a failed phase)

CI/workflow should invoke this module instead of a single integrated pytest file.
Tests never call ``compose.restart_*``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from harness.benchmark_topology import BENCHMARK_NODE_SERVICES
from harness.clients.admin import AdminClient
from harness.sim_report import git_head_sha, residency_policy
from harness.stack import compose
from harness.stack.bootstrap_benchmark import prepare_lifecycle_isolation, seed_provider_links
from harness.stack.junit_merge import merge_junit_files
from harness.stack.phase_state import (
    ENV_INVOCATION_ID,
    ENV_LIFECYCLE_PREPARED,
    ENV_STATE_PATH,
    default_state_path,
    load_state,
    new_invocation_id,
    validate_traffic_state,
)

_E2E_ROOT = Path(__file__).resolve().parents[2]
_TRAFFIC_NODE = "tests/node/test_benchmark_traffic.py"
_LIFECYCLE_NODE = "tests/node/test_benchmark_lifecycle.py"
_TARGET_MODEL = "google/gemma-3-4b-it"
_PRESSURE_MODEL = "Qwen/Qwen3.6-35B-A3B"


def _run_pytest(args: list[str], *, env: dict[str, str], cwd: Path) -> int:
    cmd = [sys.executable, "-m", "pytest", *args]
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(cwd), env=env)


def _pick_keep_provider(admin: AdminClient) -> tuple[int, list[int]]:
    nodes = admin.connected_nodes()
    if not nodes:
        raise RuntimeError("no connected workers for lifecycle prepare")
    keep = next(
        (int(n.provider_id) for n in nodes if str(n.name) == BENCHMARK_NODE_SERVICES[0]),
        int(nodes[0].provider_id),
    )
    return keep, [int(n.provider_id) for n in nodes]


def prepare_lifecycle_external(*, target: str = _TARGET_MODEL, pressure: str = _PRESSURE_MODEL) -> dict:
    """Compose-owning lifecycle SETUP (restrict + delete + orch restart)."""
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES
    with AdminClient(compose.orchestrator_url(), compose.ADMIN_KEY) as admin:
        keep, all_pids = _pick_keep_provider(admin)
        facts = prepare_lifecycle_isolation(
            admin,
            target_model=target,
            keep_provider_id=keep,
            also_restrict=[pressure],
            all_provider_ids=all_pids,
            refresh_orchestrator_cache=True,
        )
    return facts


def _archive_if_exists(path: Path, *, invocation_id: str) -> Path | None:
    """Rename prior artifact aside (keep history); do not delete evidence."""
    if not path.exists():
        return None
    archived = path.with_name(f"{path.name}.prev-{invocation_id}")
    path.replace(archived)
    return archived


def _write_runner_failure_junit(
    dest: Path,
    *,
    name: str,
    message: str,
    detail: str,
    prior_phase_junits: list[Path] | None = None,
) -> None:
    """Ensure prepare/state failures produce an error testcase (never silent)."""
    base = dest.parent / f".runner-fail-{dest.stem}.xml"
    suite = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuite name="runner" tests="1" failures="0" errors="1" skipped="0" time="0">'
        f'<testcase classname="harness.stack.run_phased_benchmark" name="{name}" time="0">'
        f'<error message="{message}">{detail}</error></testcase></testsuite>'
    )
    base.write_text(suite, encoding="utf-8")
    paths = list(prior_phase_junits or []) + [base]
    merge_junit_files(paths, dest=dest, name=f"benchmark phased ({name})")


def run_phased(
    *,
    report_path: Path,
    junit_path: Path,
    work_dir: Path | None = None,
    pytest_extra: list[str] | None = None,
) -> int:
    invocation_id = new_invocation_id()
    base = Path(work_dir) if work_dir is not None else Path(tempfile.mkdtemp(prefix="e2e-phase-"))
    base.mkdir(parents=True, exist_ok=True)
    state_path = default_state_path(base, invocation_id)
    junit_traffic = base / "junit-traffic.xml"
    junit_lifecycle = base / "junit-lifecycle.xml"
    setup_path = base / "lifecycle-setup.json"
    meta_path = base / "runner-meta.json"

    # Fixed report/junit paths from prior invocations must not look like a PASS
    # if this run aborts during prepare. Archive old files; write only on success
    # (or an explicit FAIL report from lifecycle pytest).
    archived_report = _archive_if_exists(report_path, invocation_id=invocation_id)
    archived_junit = _archive_if_exists(junit_path, invocation_id=invocation_id)

    env = os.environ.copy()
    env["E2E_BENCHMARK_TOPOLOGY"] = env.get("E2E_BENCHMARK_TOPOLOGY") or "1"
    env[ENV_INVOCATION_ID] = invocation_id
    env[ENV_STATE_PATH] = str(state_path)
    # Lifecycle pytest writes the candidate report only after real observations.
    env["E2E_SIM_REPORT_PATH"] = str(report_path)
    # Ensure source SHA consistency for emitter.
    try:
        env.setdefault("E2E_SIM_SOURCE_SHA", git_head_sha())
    except Exception as exc:  # noqa: BLE001
        print(f"runner: cannot resolve git HEAD: {exc}", file=sys.stderr)
        _write_runner_failure_junit(junit_path, name="git_head", message="git rev-parse failed", detail=str(exc))
        return 2

    meta = {
        "invocation_id": invocation_id,
        "work_dir": str(base),
        "state_path": str(state_path),
        "source_sha": env["E2E_SIM_SOURCE_SHA"],
        "residency_policy": residency_policy(),
        "report_path": str(report_path),
        "junit_path": str(junit_path),
        "archived_report": str(archived_report) if archived_report else None,
        "archived_junit": str(archived_junit) if archived_junit else None,
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"runner meta: {meta_path}", flush=True)

    extra = list(pytest_extra or [])
    traffic_rc = _run_pytest(
        [_TRAFFIC_NODE, "-v", f"--junitxml={junit_traffic}", *extra],
        env=env,
        cwd=_E2E_ROOT,
    )
    phase_paths = [junit_traffic]
    if traffic_rc != 0:
        merge_junit_files(phase_paths, dest=junit_path, name="benchmark phased (traffic failed)")
        print(f"runner: traffic phase failed rc={traffic_rc}; merged junit → {junit_path}", flush=True)
        return traffic_rc

    if not state_path.is_file():
        _write_runner_failure_junit(
            junit_path,
            name="missing_state",
            message=f"traffic passed but state missing: {state_path}",
            detail=str(state_path),
            prior_phase_junits=phase_paths,
        )
        print(f"runner: traffic passed but state missing at {state_path}", file=sys.stderr)
        return 3

    try:
        validate_traffic_state(load_state(state_path), expected_invocation_id=invocation_id)
    except Exception as exc:  # noqa: BLE001
        _write_runner_failure_junit(
            junit_path,
            name="invalid_state",
            message="invalid traffic phase state",
            detail=str(exc),
            prior_phase_junits=phase_paths,
        )
        print(f"runner: invalid traffic state: {exc}", file=sys.stderr)
        return 4

    # External compose lifecycle — not inside pytest.
    try:
        with AdminClient(compose.orchestrator_url(), compose.ADMIN_KEY) as admin:
            seed_provider_links([int(n.provider_id) for n in admin.connected_nodes()])
        facts = prepare_lifecycle_external()
        setup_path.write_text(json.dumps(facts, indent=2, default=str) + "\n", encoding="utf-8")
        # Attach setup facts into state for honest report field.
        state = load_state(state_path)
        state["lifecycle_setup"] = facts
        state["phase"] = "lifecycle_prepared"
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _write_runner_failure_junit(
            junit_path,
            name="prepare_lifecycle",
            message="external lifecycle prepare failed",
            detail=str(exc),
            prior_phase_junits=phase_paths,
        )
        # Do not leave a PASS result.json from a prior invocation at report_path
        # (already archived at start). Fail closed without writing PASS.
        print(f"runner: lifecycle prepare failed: {exc}", file=sys.stderr)
        return 5

    env[ENV_LIFECYCLE_PREPARED] = "1"
    lifecycle_rc = _run_pytest(
        [_LIFECYCLE_NODE, "-v", f"--junitxml={junit_lifecycle}", *extra],
        env=env,
        cwd=_E2E_ROOT,
    )
    phase_paths.append(junit_lifecycle)
    merge_junit_files(phase_paths, dest=junit_path, name="benchmark phased")
    if lifecycle_rc != 0:
        print(
            f"runner: lifecycle phase failed rc={lifecycle_rc}; merged junit → {junit_path}",
            flush=True,
        )
        return lifecycle_rc
    if not report_path.is_file():
        _write_runner_failure_junit(
            junit_path,
            name="missing_report",
            message="lifecycle passed but report missing",
            detail=str(report_path),
            prior_phase_junits=phase_paths,
        )
        return 6
    print(f"runner: PASS report={report_path} junit={junit_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="E2E_SIM_REPORT_PATH (default: env or ./sim-report/result.json)",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        default=None,
        help="merged junit.xml path (default: beside report)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="unique per-invocation work dir (default: fresh tempfile)",
    )
    parser.add_argument("pytest_extra", nargs="*", help="extra args forwarded to each pytest phase")
    args = parser.parse_args(argv)

    report = args.report or Path(os.environ.get("E2E_SIM_REPORT_PATH") or "sim-report/result.json")
    junit = args.junit or report.with_name("junit.xml")
    report.parent.mkdir(parents=True, exist_ok=True)

    if os.environ.get("E2E_BENCHMARK_TOPOLOGY", "").lower() not in {"1", "true", "yes"}:
        print(
            "set E2E_BENCHMARK_TOPOLOGY=1 after compose up --benchmark-topology " "and bootstrap_benchmark",
            file=sys.stderr,
        )
        return 2
    try:
        # A just-recreated orchestrator needs a moment before it answers.
        compose.wait_for_orchestrator(timeout_s=180.0)
    except TimeoutError:
        print("orchestrator not reachable — start the benchmark stack first", file=sys.stderr)
        return 2

    return run_phased(
        report_path=report,
        junit_path=junit,
        work_dir=args.work_dir,
        pytest_extra=args.pytest_extra,
    )


if __name__ == "__main__":
    sys.exit(main())
