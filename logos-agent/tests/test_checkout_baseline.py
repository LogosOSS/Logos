"""Finalization preserves the prepared parent and the remote branch lease."""

from __future__ import annotations

import json
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
    monkeypatch.setenv("LOGOS_SESSION_COMPARISON_SHA", result.data["checkout_comparison_sha"])
    monkeypatch.setenv("LOGOS_SESSION_PR_BASE_BRANCH", result.data["checkout_pr_base_branch"])
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


@pytest.mark.parametrize("service", ["logos-webservice", "logos-ui"])
def test_docs_only_continuation_rechecks_services_changed_in_prior_session(tmp_path, monkeypatch, service):
    workspace, origin, original = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    source = checkout / service / "broken-source.txt"
    source.parent.mkdir()
    source.write_text("an unresolved compile error from the previous session\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change service") == 1
    _advance(origin, "independent.txt", "another contributor\n")

    _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
    (checkout / "README.md").write_text("Documentation update only\n")
    snapshot = run_session.Result()
    run_session.run_snapshot(snapshot)

    assert run_session.os.environ["LOGOS_SESSION_COMPARISON_SHA"] == original
    assert sorted(snapshot.data["candidate_paths"]) == ["README.md", f"{service}/broken-source.txt"]
    import verify_checkout

    assert verify_checkout.selected_services(snapshot.data["candidate_paths"]) == [service]


def test_existing_pr_uses_its_nondefault_target_for_service_selection(tmp_path, monkeypatch):
    workspace, origin, original = _prepare(tmp_path, monkeypatch)
    checkout = workspace / "repo"
    _git("push", str(origin), "main:refs/heads/release", cwd=workspace / "seed")
    source = checkout / "logos-webservice" / "broken-source.txt"
    source.parent.mkdir()
    source.write_text("candidate change absent from release\n")
    assert _finalize(origin)
    assert run_session.commit_and_push(BRANCH, "Change service") == 1
    # main already contains this branch's service change. Comparing against
    # main would select no service, although the PR into release still does.
    _git("push", "origin", f"{BRANCH}:refs/heads/main", cwd=checkout)
    real_run = run_session.run

    def with_pr_target(command, **kwargs):
        if command[:3] == ["gh", "pr", "list"]:
            assert command[command.index("--head") + 1] == BRANCH
            return subprocess.CompletedProcess(command, 0, '[{"baseRefName":"release"}]', "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(run_session, "run", with_pr_target)
    monkeypatch.setenv("LOGOS_REPO_SLUG", "example/repository")
    _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
    (checkout / "README.md").write_text("Documentation update only\n")
    snapshot = run_session.Result()
    run_session.run_snapshot(snapshot)

    assert run_session.os.environ["LOGOS_SESSION_COMPARISON_SHA"] == original
    assert snapshot.data["candidate_paths"] == ["README.md", "logos-webservice/broken-source.txt"]


def test_snapshot_requires_runner_owned_comparison_proof(tmp_path, monkeypatch):
    workspace, origin, _ = _prepare(tmp_path, monkeypatch)
    (workspace / "repo" / "app.py").write_text("print('change')\n")
    monkeypatch.delenv("LOGOS_SESSION_COMPARISON_SHA")
    with pytest.raises(RuntimeError, match="LOGOS_SESSION_COMPARISON_SHA is not set"):
        run_session.run_snapshot(run_session.Result())


@pytest.mark.parametrize("target", ["main", "release"])
@pytest.mark.parametrize("retry_same_session", [False, True])
def test_failed_pr_creation_preserves_publication_proof_and_original_target(
    tmp_path, monkeypatch, target, retry_same_session
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    origin = _make_origin(workspace)
    if target != "main":
        _git("push", str(origin), f"main:refs/heads/{target}", cwd=workspace / "seed")
    _prepare(tmp_path, monkeypatch, base=target, origin=origin)
    checkout = workspace / "repo"
    (checkout / "app.py").write_text("print('published work')\n")
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("LOGOS_ARTIFACT_DIR", str(artifacts))
    monkeypatch.setenv("LOGOS_SESSION_TASK", "Update the application")
    monkeypatch.setenv("LOGOS_SESSION_OPEN_PR", "1")
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "passed")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("LOGOS_REPO_SLUG", "example/repository")
    snapshot = run_session.Result()
    run_session.run_snapshot(snapshot)
    snapshot.write()
    monkeypatch.setenv("LOGOS_SESSION_VERIFIED_TREE", snapshot.data["candidate_tree"])
    real_run = run_session.run
    creation_fails = True
    creates = []

    def github_api(command, **kwargs):
        if command[:3] == ["gh", "pr", "create"]:
            creates.append(command)
            recorded = json.loads((artifacts / "result.json").read_text())
            remote = _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip()
            assert recorded["pushed_sha"] == remote
            assert recorded["published_tree"] == snapshot.data["candidate_tree"]
            assert recorded["pr_base_branch"] == target
            assert command[command.index("--base") + 1] == target
            return subprocess.CompletedProcess(
                command, 1 if creation_fails else 0, "" if creation_fails else "https://example/pr/54\n", ""
            )
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(command, 1, "{}", "")
        if command[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(command, 0, "[]", "")
        return real_run(command, **kwargs)

    monkeypatch.setattr(run_session, "run", github_api)
    first = run_session.Result()
    with pytest.raises(RuntimeError, match="could not publish the pull request"):
        run_session.run_finalize(first)
    proof = json.loads((artifacts / "result.json").read_text())
    published = proof["pushed_sha"]
    assert proof["committed"] is True
    assert proof["pr_url"] is None

    if not retry_same_session:
        # The runner retains the trusted proof even when the helper exits
        # nonzero, then prepares the next session from the published branch.
        monkeypatch.setenv("LOGOS_SESSION_PR_BASE_BRANCH", proof["pr_base_branch"])
        _prepare(tmp_path, monkeypatch, base=BRANCH, origin=origin)
        assert _git("rev-parse", "HEAD", cwd=checkout).stdout.strip() == published
        assert (checkout / "app.py").read_text() == "print('published work')\n"
    creation_fails = False
    snapshot = run_session.Result()
    run_session.run_snapshot(snapshot)
    snapshot.write()
    monkeypatch.setenv("LOGOS_SESSION_VERIFIED_TREE", snapshot.data["candidate_tree"])
    retry = run_session.Result()
    run_session.run_finalize(retry)

    assert retry.data["pr_url"] == "https://example/pr/54"
    assert retry.data["pushed_sha"] == published
    assert retry.data["committed"] is False
    assert len(creates) == 2
    assert _git("--git-dir", str(origin), "rev-parse", BRANCH, cwd=workspace).stdout.strip() == published
