"""Finalization preserves the prepared parent and the remote branch lease."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.test_run_session import _git, _make_origin, _patch_workspace, run_session

BRANCH = "logos-agent/work/session-54"


def _prepare(tmp_path, monkeypatch, *, base="main", origin=None):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    origin = origin or _make_origin(workspace)
    _patch_workspace(monkeypatch, workspace)
    monkeypatch.setenv("LOGOS_REPO_URL", f"file://{origin}")
    monkeypatch.setenv("LOGOS_SESSION_BASE_BRANCH", base)
    monkeypatch.setenv("LOGOS_SESSION_BRANCH", BRANCH)
    result = run_session.Result()
    run_session.run_prepare(result)
    monkeypatch.setenv("LOGOS_SESSION_CHECKOUT_SHA", result.data["checkout_sha"])
    monkeypatch.setenv("LOGOS_SESSION_BRANCH_SHA", result.data["checkout_branch_sha"])
    return workspace, origin, result.data["checkout_sha"]


def _advance(origin: Path, path: str, content: str, *, branch="main") -> str:
    seed = origin.parent / "seed"
    _git("fetch", str(origin), branch, cwd=seed)
    _git("checkout", "-B", branch, "FETCH_HEAD", cwd=seed)
    (seed / path).write_text(content)
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "Independent work", cwd=seed)
    _git("push", str(origin), branch, cwd=seed)
    return _git("rev-parse", "HEAD", cwd=seed).stdout.strip()


def _finalize(origin: Path) -> bool:
    return run_session.finalize_checkout(f"file://{origin}", "main", BRANCH, "test-token")


def test_advancing_main_is_not_reverted_by_the_agent_pull_request(tmp_path, monkeypatch):
    workspace, origin, baseline = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    newest_main = _advance(origin, "independent.txt", "another contributor\n")

    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change application") == 1
    assert _git("rev-parse", "HEAD^", cwd=checkout).stdout.strip() == baseline
    assert _git("--git-dir", str(origin), "rev-parse", "main", cwd=workspace).stdout.strip() == newest_main
    assert _git("--git-dir", str(origin), "diff", "--name-only", f"main...{BRANCH}", cwd=workspace).stdout == "app.py\n"
    _git("fetch", "origin", "main", cwd=checkout)
    _git("merge", "--no-edit", "FETCH_HEAD", cwd=checkout)
    assert (checkout / "independent.txt").read_text() == "another contributor\n"
    assert (checkout / "app.py").read_text() == "print('agent change')\n"


def test_overlapping_main_edit_remains_a_real_merge_conflict(tmp_path, monkeypatch):
    workspace, origin, baseline = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    newest_main = _advance(origin, "app.py", "print('independent change')\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change application") == 1
    assert _git("rev-parse", "HEAD^", cwd=checkout).stdout.strip() == baseline
    _git("fetch", "origin", "main", cwd=checkout)
    merged = subprocess.run(["git", "merge", "--no-edit", "FETCH_HEAD"], cwd=checkout, capture_output=True, text=True)
    assert merged.returncode == 1
    assert "CONFLICT" in merged.stdout
    assert _git("--git-dir", str(origin), "rev-parse", "main", cwd=workspace).stdout.strip() == newest_main


def test_repeated_finalization_keeps_the_identical_pushed_snapshot(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change application") == 1
    first = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    _advance(origin, "independent.txt", "another contributor\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change application") == 0
    assert _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip() == first


def test_unchanged_checkout_does_not_claim_verification_of_a_concurrent_branch_tip(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    _git("push", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=checkout)
    _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
    external = _advance(origin, "external.txt", "reviewer change\n", branch=BRANCH)
    assert _finalize(origin)
    with pytest.raises(RuntimeError, match="unverified branch tip"):
        run_session.commit_and_push(BRANCH, "No changes")
    assert _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip() == external


def test_continued_session_keeps_existing_branch_work(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "first.txt").write_text("first session\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Add first change") == 1
    first = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    monkeypatch.setenv("LOGOS_SESSION_CONTINUE", "1")
    _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
    (checkout / "second.txt").write_text("second session\n")
    _advance(origin, "independent.txt", "another contributor\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Add second change") == 1
    assert _git("rev-parse", "HEAD^", cwd=checkout).stdout.strip() == first
    assert _git("diff", "--name-only", "HEAD^", "HEAD", cwd=checkout).stdout == "second.txt\n"
    assert (checkout / "first.txt").read_text() == "first session\n"


def test_concurrent_changes_to_existing_pr_branch_are_not_overwritten(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "first.txt").write_text("first session\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Add first change") == 1
    _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
    (checkout / "second.txt").write_text("second session\n")
    external = _advance(origin, "external.txt", "reviewer change\n", branch=BRANCH)
    assert _finalize(origin)
    with pytest.raises(RuntimeError, match="remote session branch changed"):
        run_session.commit_and_push(BRANCH, "Add second change")
    assert _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip() == external


def test_branch_race_after_finalize_is_rejected_by_the_explicit_push_lease(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    assert _finalize(origin)
    _git("push", str(origin), f"main:refs/heads/{BRANCH}", cwd=workspace / "seed")
    external = _advance(origin, "external.txt", "reviewer change\n", branch=BRANCH)
    with pytest.raises(RuntimeError, match="command failed"):
        run_session.commit_and_push(BRANCH, "Change application")
    assert _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip() == external


@pytest.mark.parametrize("missing", ["LOGOS_SESSION_CHECKOUT_SHA", "LOGOS_SESSION_BRANCH_SHA"])
def test_missing_runner_proof_fails_closed(tmp_path, monkeypatch, missing):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    (workspace / "repo" / "app.py").write_text("print('agent change')\n")
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match="no trusted prepared"):
        _finalize(origin)


def test_local_refs_and_objects_cannot_replace_the_trusted_parent(tmp_path, monkeypatch):
    workspace, origin, baseline = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-m", "Agent-created parent", cwd=checkout)
    fake = _git("rev-parse", "HEAD", cwd=checkout).stdout.strip()
    (checkout / ".git" / "objects" / "info" / "alternates").write_text("/does/not/exist\n")
    assert _finalize(origin)
    assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == baseline
    assert not (checkout / ".git" / "objects" / "info" / "alternates").exists()
    missing = subprocess.run(["git", "cat-file", "-t", fake], cwd=checkout, capture_output=True, text=True)
    assert missing.returncode != 0
    assert run_session.commit_and_push(BRANCH, "Change application") == 1


def test_existing_branch_must_be_the_prepared_base(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    (workspace / "repo" / "first.txt").write_text("first session\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Add first change") == 1
    monkeypatch.setenv("LOGOS_SESSION_BASE_BRANCH", "main")
    with pytest.raises(RuntimeError, match="existing session branch differs"):
        run_session.run_prepare(run_session.Result())


def test_pinned_baseline_remains_fetchable_beyond_the_shallow_window(tmp_path, monkeypatch):
    workspace, origin, baseline = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('agent change')\n")
    seed = workspace / "seed"
    tree = _git("rev-parse", "HEAD^{tree}", cwd=seed).stdout.strip()
    tip = baseline
    for number in range(55):
        tip = _git("commit-tree", tree, "-p", tip, "-m", f"Main update {number}", cwd=seed).stdout.strip()
    _git("update-ref", "refs/heads/main", tip, cwd=seed)
    _git("push", str(origin), "main", cwd=seed)

    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change application") == 1
    assert _git("rev-parse", "HEAD^", cwd=checkout).stdout.strip() == baseline
    assert _git("--git-dir", str(origin), "diff", "--name-only", f"main...{BRANCH}", cwd=workspace).stdout == "app.py\n"
