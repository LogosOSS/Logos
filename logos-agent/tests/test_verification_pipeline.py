"""The publication gate keeps trusted evidence outside repository build code."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import docker_engine, sessions

SHA = "a" * 40
TREE = "b" * 40


def baseline(session_id=7):
    path = sessions.state_dir(session_id) / "checkout_baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"checkout_sha": SHA, "branch_sha": "", "comparison_sha": SHA, "pr_base_branch": "main"})
    )
    return path


async def active(_sid):
    return {"id": 7, "status": "finalizing"}


async def no_event(*_args, **_kwargs):
    pass


def test_baseline_is_captured_before_the_agent_can_replace_the_result(monkeypatch):
    manager = sessions.SessionManager()
    result = {
        "checkout_sha": SHA,
        "checkout_branch_sha": "",
        "checkout_comparison_sha": "c" * 40,
        "checkout_pr_base_branch": "main",
    }
    monkeypatch.setattr(manager, "_read_result", lambda _sid: result)
    manager._capture_checkout_baseline(7)
    result["checkout_sha"] = "f" * 40
    proof = json.loads((sessions.state_dir(7) / "checkout_baseline.json").read_text())
    assert proof == {"checkout_sha": SHA, "branch_sha": "", "comparison_sha": "c" * 40, "pr_base_branch": "main"}


@pytest.mark.parametrize("result", [{}, {"checkout_sha": "HEAD", "checkout_branch_sha": ""}])
def test_prepare_fails_without_a_valid_baseline(monkeypatch, result):
    manager = sessions.SessionManager()
    monkeypatch.setattr(manager, "_read_result", lambda _sid: result)
    with pytest.raises(RuntimeError, match="original commit"):
        manager._capture_checkout_baseline(7)


def test_prepare_fails_without_the_full_pr_comparison_boundary(monkeypatch):
    manager = sessions.SessionManager()
    monkeypatch.setattr(manager, "_read_result", lambda _sid: {"checkout_sha": SHA, "checkout_branch_sha": ""})
    with pytest.raises(RuntimeError, match="PR comparison commit"):
        manager._capture_checkout_baseline(7)


@pytest.mark.parametrize("exit_code,status", [(0, "passed"), (1, "failed"), (124, "failed")])
async def test_checks_receive_no_credentials_and_runner_binds_outcome_to_tree(monkeypatch, exit_code, status):
    manager = sessions.SessionManager()
    baseline()
    calls = []

    async def helper(**kwargs):
        calls.append(kwargs)
        if kwargs["phase"] == "verify":
            manager._last_helper_output[7] = "compiler diagnostics"
            return exit_code
        return 0

    monkeypatch.setattr(manager, "_run_helper", helper)
    monkeypatch.setattr(
        manager, "_read_result", lambda _sid: {"candidate_tree": TREE, "candidate_paths": ["logos-ui/a.ts"]}
    )
    monkeypatch.setattr(sessions.db, "get_session", active)
    monkeypatch.setattr(sessions.db, "add_event", no_event)
    env = await manager._verify_before_push(
        7, {"volume_name": "workspace"}, {"GITHUB_TOKEN": "secret", "GH_TOKEN": "secret"}, "/artifacts/7"
    )
    assert [call["phase"] for call in calls] == ["snapshot", "verify"]
    assert calls[0]["env"]["LOGOS_SESSION_CHECKOUT_SHA"] == SHA
    assert calls[0]["env"]["LOGOS_SESSION_COMPARISON_SHA"] == SHA
    assert calls[1]["env"] == {
        "LOGOS_SESSION_PHASE": "verify",
        "LOGOS_VERIFICATION_PATHS": '["logos-ui/a.ts"]',
        "LOGOS_VERIFICATION_TREE": TREE,
    }
    assert env["LOGOS_SESSION_VERIFIED_TREE"] == TREE
    assert env["LOGOS_SESSION_VERIFICATION"] == status
    proof = json.loads((sessions.state_dir(7) / "verification.json").read_text())
    assert proof["status"] == status
    assert proof["tree"] == TREE


async def test_missing_protected_baseline_never_starts_a_helper(monkeypatch):
    manager = sessions.SessionManager()

    async def unexpected(**_kwargs):
        pytest.fail("must not run a helper without baseline proof")

    monkeypatch.setattr(manager, "_run_helper", unexpected)
    with pytest.raises(RuntimeError, match="missing trusted checkout baseline"):
        await manager._verify_before_push(7, {}, {}, "/artifacts/7")


async def test_snapshot_failure_never_runs_repository_build_code(monkeypatch):
    manager = sessions.SessionManager()
    baseline()
    calls = []

    async def helper(**kwargs):
        calls.append(kwargs["phase"])
        return 1

    monkeypatch.setattr(manager, "_run_helper", helper)
    with pytest.raises(RuntimeError, match="candidate snapshot failed"):
        await manager._verify_before_push(7, {"volume_name": "workspace"}, {}, "/artifacts/7")
    assert calls == ["snapshot"]


async def test_cancel_after_checks_prevents_publication(monkeypatch):
    manager = sessions.SessionManager()
    baseline()
    rows = iter([{"status": "finalizing"}, {"status": "cancelled"}])

    async def row(_sid):
        return next(rows)

    async def helper(**_kwargs):
        return 0

    monkeypatch.setattr(manager, "_run_helper", helper)
    monkeypatch.setattr(manager, "_read_result", lambda _sid: {"candidate_tree": TREE, "candidate_paths": []})
    monkeypatch.setattr(sessions.db, "get_session", row)
    monkeypatch.setattr(sessions.db, "add_event", no_event)
    with pytest.raises(RuntimeError, match="cancelled before publishing"):
        await manager._verify_before_push(7, {"volume_name": "workspace"}, {}, "/artifacts/7")


async def test_failed_verification_retries_from_published_draft_and_cannot_deploy(monkeypatch):
    manager = sessions.SessionManager()
    updates = []

    async def session(_sid):
        return {"id": 7, "workspace_id": 3, "branch_name": "agent/7", "status": "finalizing"}

    async def workspace(_wid):
        return {"base_branch": "main", "volume_name": "workspace"}

    async def verified(_sid, _workspace, env, _path):
        return {
            **env,
            "LOGOS_SESSION_VERIFICATION": "failed",
            "LOGOS_SESSION_VERIFICATION_OUTPUT": "compile error",
            "LOGOS_SESSION_VERIFIED_TREE": TREE,
            "LOGOS_SESSION_PR_BASE_BRANCH": "main",
        }

    async def helper(**_kwargs):
        return 0

    async def mount(_volume):
        return "/artifacts"

    async def set_base(wid, branch):
        updates.append((wid, branch))

    monkeypatch.setattr(sessions.db, "get_session", session)
    monkeypatch.setattr(sessions.db, "get_workspace", workspace)
    monkeypatch.setattr(sessions.db, "set_workspace_base_branch", set_base)
    monkeypatch.setattr(sessions.docker_engine, "volume_mountpoint", mount)
    monkeypatch.setattr(manager, "_verify_before_push", verified)
    monkeypatch.setattr(manager, "_run_helper", helper)
    monkeypatch.setattr(
        manager, "_read_result", lambda _sid: {"pushed_sha": SHA, "published_tree": TREE, "pr_base_branch": "main"}
    )
    assert await manager._finalize(7) is False
    assert updates == [(3, "agent/7")]
    assert "compile error" in manager._last_helper_output[7]


@pytest.mark.parametrize("target", ["main", "release"])
async def test_failed_pr_creation_retains_published_branch_and_original_target_for_next_session(monkeypatch, target):
    manager = sessions.SessionManager()
    session = {"id": 7, "workspace_id": 3, "branch_name": "agent/7", "status": "finalizing"}
    workspace = {"base_branch": target, "volume_name": "workspace"}
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(sessions.db, "get_workspace", AsyncMock(return_value=workspace))
    set_base = AsyncMock()
    monkeypatch.setattr(sessions.db, "set_workspace_base_branch", set_base)
    monkeypatch.setattr(sessions.docker_engine, "volume_mountpoint", AsyncMock(return_value="/artifacts"))
    monkeypatch.setattr(
        manager,
        "_verify_before_push",
        AsyncMock(
            return_value={
                "LOGOS_SESSION_VERIFICATION": "passed",
                "LOGOS_SESSION_VERIFIED_TREE": TREE,
                "LOGOS_SESSION_PR_BASE_BRANCH": target,
            }
        ),
    )
    monkeypatch.setattr(manager, "_run_helper", AsyncMock(return_value=1))
    monkeypatch.setattr(
        manager,
        "_read_result",
        lambda _sid: {
            "pushed_sha": SHA,
            "published_tree": TREE,
            "pr_base_branch": target,
        },
    )
    assert await manager._finalize(7) is False
    set_base.assert_awaited_once_with(3, "agent/7")
    assert json.loads(sessions.publication_path(3).read_text()) == {
        "branch": "agent/7",
        "pushed_sha": SHA,
        "tree": TREE,
        "pr_base_branch": target,
    }

    # A new manager also recovers if the DB update was lost after the receipt.
    next_manager = sessions.SessionManager()
    helper = AsyncMock(return_value=0)
    monkeypatch.setattr(next_manager, "_run_helper", helper)
    monkeypatch.setattr(
        next_manager,
        "_read_result",
        lambda _sid: {
            "checkout_sha": SHA,
            "checkout_branch_sha": SHA,
            "checkout_comparison_sha": "c" * 40,
            "checkout_pr_base_branch": target,
        },
    )
    await next_manager._prepare_checkout({**session, "id": 8}, workspace, "agent/7", "/artifacts/8")
    env = helper.call_args.kwargs["env"]
    assert env["LOGOS_SESSION_BASE_BRANCH"] == "agent/7"
    assert env["LOGOS_SESSION_PR_BASE_BRANCH"] == target
    proof = json.loads((sessions.state_dir(8) / "checkout_baseline.json").read_text())
    assert proof["pr_base_branch"] == target


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"pushed_sha": SHA, "published_tree": "c" * 40, "pr_base_branch": "main"},
        {"pushed_sha": SHA, "published_tree": TREE, "pr_base_branch": "other"},
    ],
)
async def test_failed_finalizer_without_matching_publication_receipt_keeps_workspace_base(monkeypatch, receipt):
    manager = sessions.SessionManager()
    monkeypatch.setattr(
        sessions.db,
        "get_session",
        AsyncMock(
            return_value={
                "id": 7,
                "workspace_id": 3,
                "branch_name": "agent/7",
                "status": "finalizing",
            }
        ),
    )
    monkeypatch.setattr(
        sessions.db,
        "get_workspace",
        AsyncMock(
            return_value={
                "base_branch": "main",
                "volume_name": "workspace",
            }
        ),
    )
    set_base = AsyncMock()
    monkeypatch.setattr(sessions.db, "set_workspace_base_branch", set_base)
    monkeypatch.setattr(sessions.docker_engine, "volume_mountpoint", AsyncMock(return_value="/artifacts"))
    monkeypatch.setattr(
        manager,
        "_verify_before_push",
        AsyncMock(
            return_value={
                "LOGOS_SESSION_VERIFICATION": "passed",
                "LOGOS_SESSION_VERIFIED_TREE": TREE,
                "LOGOS_SESSION_PR_BASE_BRANCH": "main",
            }
        ),
    )
    monkeypatch.setattr(manager, "_run_helper", AsyncMock(return_value=1))
    monkeypatch.setattr(manager, "_read_result", lambda _sid: receipt)
    assert await manager._finalize(7) is False
    set_base.assert_not_awaited()
    assert not sessions.publication_path(3).exists()


async def test_verify_container_cannot_modify_candidate_or_access_network(monkeypatch):
    payloads = []

    async def request(_method, _path, **kwargs):
        payloads.append(kwargs["json"])
        return SimpleNamespace(json=lambda: {"Id": "verify"})

    monkeypatch.setattr(docker_engine, "_request", request)
    await docker_engine.create_session_container(
        name="verify",
        image="workspace",
        env={"LOGOS_SESSION_PHASE": "verify"},
        workspace_volume="workspace",
        artifact_host_path="/artifacts/7",
        session_id=7,
        network="egress",
    )
    payload = payloads[0]
    assert payload["HostConfig"]["Binds"] == ["workspace:/workspace:ro", "/artifacts/7:/artifacts:ro"]
    assert payload["HostConfig"]["NetworkMode"] == "none"
    assert payload["Entrypoint"] == ["python3", "/usr/local/lib/verify_checkout.py"]
    assert payload["HostConfig"]["ReadonlyRootfs"]
    assert payload["HostConfig"]["CapDrop"] == ["ALL"]


@pytest.mark.parametrize("extra", [{"GITHUB_TOKEN": "secret"}, {"GH_TOKEN": "secret"}])
async def test_verify_container_refuses_credential_environment(extra):
    with pytest.raises(ValueError, match="credentials"):
        await docker_engine.create_session_container(
            name="verify",
            image="workspace",
            env={"LOGOS_SESSION_PHASE": "verify", **extra},
            workspace_volume="workspace",
            artifact_host_path="/artifacts/7",
            session_id=7,
        )


async def test_verify_container_refuses_credential_mounts():
    with pytest.raises(ValueError, match="credentials"):
        await docker_engine.create_session_container(
            name="verify",
            image="workspace",
            env={"LOGOS_SESSION_PHASE": "verify"},
            workspace_volume="workspace",
            artifact_host_path="/artifacts/7",
            session_id=7,
            ssh_key_host_path="/key",
        )


@pytest.fixture
def finalizer(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "verified_finalizer", Path(__file__).parents[1] / "workspace/run_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("LOGOS_SESSION_BRANCH", "agent/7")
    monkeypatch.setenv("LOGOS_SESSION_PR_BASE_BRANCH", "main")
    monkeypatch.setenv("LOGOS_SESSION_TASK", "Fix compilation")
    monkeypatch.setenv("LOGOS_REPO_URL", "https://github.com/example/repo")
    monkeypatch.setenv("LOGOS_REPO_SLUG", "example/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "passed")
    monkeypatch.setenv("LOGOS_SESSION_VERIFIED_TREE", TREE)
    monkeypatch.setenv("LOGOS_SESSION_CHECKOUT_SHA", SHA)
    monkeypatch.setenv("LOGOS_SESSION_BRANCH_SHA", SHA)
    monkeypatch.setenv("LOGOS_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(module, "finalize_checkout", lambda *_args: True)
    monkeypatch.setattr(module, "_ref_sha", lambda *_args: SHA)
    monkeypatch.setattr(module, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=TREE, returncode=0))
    return module


def test_finalizer_refuses_changed_tree_even_with_passed_check(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFIED_TREE", "c" * 40)
    monkeypatch.setattr(finalizer, "commit_and_push", lambda *_args: pytest.fail("changed tree must not be pushed"))
    with pytest.raises(RuntimeError, match="changed after verification"):
        finalizer.run_finalize(finalizer.Result())


def test_failed_check_marks_existing_pr_draft_before_push_and_reports_it(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "failed")
    order = []
    monkeypatch.setattr(finalizer, "_draft_existing_pull_request", lambda *_args: order.append("draft"))
    monkeypatch.setattr(finalizer, "commit_and_push", lambda *_args: order.append("push") or 1)
    monkeypatch.setattr(finalizer, "open_pull_request", lambda *_args: order.append("report") or "https://example/pr/1")
    result = finalizer.Result()
    finalizer.run_finalize(result)
    assert order == ["draft", "push", "report"]
    assert result.data["verification"] == "failed"


def test_changed_branch_lease_prevents_all_pr_metadata_changes(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "failed")
    monkeypatch.setenv("LOGOS_SESSION_BRANCH_SHA", "c" * 40)
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["git", "write-tree"]:
            return SimpleNamespace(stdout=TREE, returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(finalizer, "run", run)
    with pytest.raises(RuntimeError, match="remote session branch changed"):
        finalizer.run_finalize(finalizer.Result())
    assert all(command[0] == "git" for command in calls)
    assert not any(command[:2] == ["git", "push"] for command in calls)


def test_exact_published_snapshot_allows_draft_metadata_recovery(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "failed")
    monkeypatch.setenv("LOGOS_SESSION_BRANCH_SHA", "")
    calls = []
    published = "d" * 40

    def ref_sha(ref):
        return TREE if ref.endswith("^{tree}") else published

    def run(command, **_kwargs):
        stdout = f"{published} {SHA}" if command[:2] == ["git", "rev-list"] else TREE
        return SimpleNamespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(finalizer, "_ref_sha", ref_sha)
    monkeypatch.setattr(finalizer, "run", run)
    monkeypatch.setattr(finalizer, "_draft_existing_pull_request", lambda *_args: calls.append("draft"))
    monkeypatch.setattr(finalizer, "commit_and_push", lambda *_args: 0)
    monkeypatch.setattr(finalizer, "open_pull_request", lambda *_args: "https://example/pr/1")
    finalizer.run_finalize(finalizer.Result())
    assert calls == ["draft"]


def test_forged_result_does_not_become_a_publication_or_verification_proof(finalizer, tmp_path):
    (tmp_path / "result.json").write_text(json.dumps({"pr_url": "forged", "verification": "passed", "tokens_in": 12}))
    result = finalizer.Result()
    finalizer._restore_agent_result(result)
    assert result.data["pr_url"] is None
    assert "verification" not in result.data
    assert result.data["tokens_in"] == 12


def test_failed_check_creates_draft_with_failure_output(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "failed")
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION_OUTPUT", "cannot find symbol")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="https://example/pr/1\n", returncode=0)

    monkeypatch.setattr(finalizer, "run", run)
    finalizer.open_pull_request("agent/7", "main", "Fix compilation")
    assert "--draft" in calls[0]
    assert "cannot find symbol" in calls[0][calls[0].index("--body") + 1]


def test_existing_pr_gets_draft_and_failure_diagnostics_before_publishing(finalizer, monkeypatch):
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION", "failed")
    monkeypatch.setenv("LOGOS_SESSION_VERIFICATION_OUTPUT", "test compilation failed")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='[{"number": 7, "isDraft": false}]', returncode=0)

    monkeypatch.setattr(finalizer, "run", run)
    finalizer._draft_existing_pull_request("agent/7")
    assert calls[1][:4] == ["gh", "pr", "ready", "7"]
    assert "--undo" in calls[1]
    assert calls[2][:4] == ["gh", "pr", "edit", "7"]
    assert "test compilation failed" in calls[2][calls[2].index("--body") + 1]
