"""Persistent SSH trust is writable only without session-controlled mounts."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from app import docker_engine, repo_credentials, sessions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace"))
import run_session  # noqa: E402


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/org/private.git", "ssh://git@github.com/org/private.git"),
        ("https://git.example.org/team/repo.git", "ssh://git@git.example.org/team/repo.git"),
        ("git@git.example.org:team/repo.git", "git@git.example.org:team/repo.git"),
        ("ssh://alice@git.example.org:2222/team/repo.git", "ssh://alice@git.example.org:2222/team/repo.git"),
    ],
)
async def test_deploy_key_checkout_keeps_the_repository_host(monkeypatch, tmp_path, url, expected):
    manager = sessions.SessionManager()
    helper = AsyncMock(return_value=0)
    monkeypatch.setattr(
        manager,
        "_read_result",
        lambda sid: {
            "checkout_sha": "a" * 40,
            "checkout_branch_sha": "",
            "checkout_comparison_sha": "a" * 40,
            "checkout_pr_base_branch": "main",
        },
    )
    monkeypatch.setattr(manager, "_run_helper", helper)
    monkeypatch.setattr(repo_credentials, "load_deploy_key_pem", AsyncMock(return_value="private key"))
    monkeypatch.setattr(sessions, "_give_to_session_user", lambda path: None)

    async def inspect_helper(**kwargs):
        key = sessions.state_dir(7) / "deploy_key"
        assert key.read_text() == "private key\n"
        assert not key.is_relative_to(tmp_path / "artifacts")
        assert kwargs["env"]["LOGOS_REPO_URL"] == expected
        assert kwargs["env"]["LOGOS_SSH_DEPLOY_KEY"] == "1"
        assert "GITHUB_TOKEN" not in kwargs["env"]
        return 0

    helper.side_effect = inspect_helper
    await manager._prepare_checkout(
        {"id": 7, "trigger_kind": "analysis", "team_repository_id": 42, "repo_url": url},
        {"base_branch": "main", "volume_name": "workspace"},
        "analysis",
        str(tmp_path / "artifacts" / "7"),
    )
    assert not (sessions.state_dir(7) / "deploy_key").exists()


@pytest.mark.parametrize("phase", ["prepare", "finalize"])
async def test_ssh_helpers_share_persistent_trust_and_only_checkout_reads_it(monkeypatch, tmp_path, phase):
    manager = sessions.SessionManager()
    containers = []
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value={"status": "finalizing"}))
    monkeypatch.setattr(sessions, "_give_to_session_user", lambda path: None)
    monkeypatch.setattr(docker_engine, "volume_mountpoint", AsyncMock(return_value="/daemon/state"))

    async def request(method, path, **kwargs):
        containers.append(kwargs["json"])
        return type("Response", (), {"json": lambda self: {"Id": str(len(containers))}})()

    monkeypatch.setattr(docker_engine, "_request", request)
    monkeypatch.setattr(docker_engine, "start_container", AsyncMock())
    monkeypatch.setattr(docker_engine, "wait_container", AsyncMock(return_value=0))
    monkeypatch.setattr(docker_engine, "remove_container", AsyncMock())
    for sid in (7, 8):
        assert (
            await manager._run_helper(
                phase=phase,
                session_id=sid,
                env={
                    "LOGOS_SESSION_PHASE": phase,
                    "LOGOS_REPO_URL": "ssh://git@git.example.org:2222/team/repo.git",
                    "LOGOS_SSH_DEPLOY_KEY": "1",
                },
                workspace_volume=f"workspace-{sid}",
                artifact_host_path=f"/daemon/artifacts/{sid}",
            )
            == 0
        )
        known_hosts = Path(sessions.settings.state_root) / "ssh_known_hosts"
        assert known_hosts.is_file()
        assert not known_hosts.is_relative_to(sessions.state_dir(sid))
        assert not known_hosts.is_relative_to(Path(sessions.settings.artifact_root))
        if sid == 7:
            known_hosts.write_text("git.example.org ssh-ed25519 remembered-key\n")
        else:
            assert "remembered-key" in known_hosts.read_text()

    for trust, checkout in (containers[:2], containers[2:]):
        assert trust["WorkingDir"] == "/tmp"
        assert trust["HostConfig"]["Binds"] == [
            "/daemon/state/ssh_known_hosts:/logos/ssh_known_hosts:rw",
            f"/daemon/state/{trust['Labels']['logos.agent.session']}/deploy_key:/logos/deploy_key:ro",
        ]
        assert "/daemon/state/ssh_known_hosts:/logos/ssh_known_hosts:ro" in checkout["HostConfig"]["Binds"]
        for container in (trust, checkout):
            env = dict(entry.split("=", 1) for entry in container["Env"])
            command = shlex.split(env["GIT_SSH_COMMAND"])
            assert "StrictHostKeyChecking=accept-new" in command
            assert "UserKnownHostsFile=/logos/ssh_known_hosts" in command
            assert "UpdateHostKeys=no" in command
            assert "IdentitiesOnly=yes" in command
            assert container["HostConfig"]["ReadonlyRootfs"] is True

    containers.clear()
    await docker_engine.create_session_container(
        name="agent",
        image="workspace",
        env={"LOGOS_SESSION_PHASE": "agent"},
        workspace_volume="workspace",
        artifact_host_path="/daemon/artifacts/7",
        session_id=7,
    )
    assert all("ssh_known_hosts" not in bind for bind in containers[0]["HostConfig"]["Binds"])


@pytest.mark.parametrize("phase", ["ssh-trust", "prepare"])
def test_changed_host_key_fails_with_clear_log_and_result(monkeypatch, tmp_path, capsys, phase):
    known_hosts = tmp_path / "runner-state" / "ssh_known_hosts"
    known_hosts.parent.mkdir()
    known_hosts.write_text("git.example.org ssh-ed25519 original-key\n")
    monkeypatch.setattr(run_session, "SSH_KNOWN_HOSTS", known_hosts)
    monkeypatch.setenv("LOGOS_SESSION_PHASE", phase)
    monkeypatch.setenv("LOGOS_REPO_URL", "git@git.example.org:team/repo.git")
    monkeypatch.setenv("LOGOS_ARTIFACT_DIR", str(tmp_path / "result"))
    monkeypatch.setenv("LOGOS_SESSION_BRANCH", "analysis")
    monkeypatch.setenv("GIT_SSH_COMMAND", sessions.SSH_COMMAND)

    def changed_key(cmd, **kwargs):
        assert "StrictHostKeyChecking=accept-new" in kwargs["env"]["GIT_SSH_COMMAND"]
        return subprocess.CompletedProcess(
            cmd, 128, "", "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.\n"
        )

    monkeypatch.setattr(run_session.subprocess, "run", changed_key)
    monkeypatch.setattr(run_session, "prepare_checkout", lambda *args: run_session.run(["git", "fetch", "origin"]))
    assert run_session.main() == 1
    assert known_hosts.read_text() == "git.example.org ssh-ed25519 original-key\n"
    result = json.loads((tmp_path / "result" / "result.json").read_text())
    assert "SSH host key verification failed" in result["error"]
    assert "SSH host key verification failed" in capsys.readouterr().err


async def test_trust_failure_stops_checkout_and_reports_the_reason(monkeypatch, tmp_path):
    manager = sessions.SessionManager()
    monkeypatch.setattr(sessions, "_give_to_session_user", lambda path: None)
    monkeypatch.setattr(docker_engine, "volume_mountpoint", AsyncMock(return_value="/daemon/state"))
    create = AsyncMock(return_value="trust-container")
    monkeypatch.setattr(docker_engine, "create_session_container", create)
    monkeypatch.setattr(docker_engine, "start_container", AsyncMock())
    monkeypatch.setattr(docker_engine, "wait_container", AsyncMock(return_value=1))
    monkeypatch.setattr(docker_engine, "remove_container", AsyncMock())

    async def logs(*args, **kwargs):
        yield "SSH host key verification failed: the git host key changed or is not trusted"

    monkeypatch.setattr(docker_engine, "stream_logs", logs)
    with pytest.raises(RuntimeError, match="SSH host key verification failed") as failure:
        await manager._prepare_checkout(
            {"id": 7, "repo_url": "git@git.example.org:team/repo.git"},
            {"base_branch": "main", "volume_name": "workspace"},
            "analysis",
            str(tmp_path / "artifacts"),
        )
    create.assert_awaited_once()
    assert create.call_args.kwargs["env"]["LOGOS_SESSION_PHASE"] == "ssh-trust"

    transition = AsyncMock(return_value=True)
    event = AsyncMock()
    monkeypatch.setattr(sessions.db, "transition_session", transition)
    monkeypatch.setattr(sessions.db, "add_event", event)
    monkeypatch.setattr(sessions.db, "get_session", AsyncMock(return_value={}))
    monkeypatch.setattr(manager, "_read_result", lambda sid: {})
    monkeypatch.setattr(manager, "_react", AsyncMock())
    monkeypatch.setattr(manager, "_post_reply", AsyncMock())
    monkeypatch.setattr(manager, "_cleanup_container", AsyncMock())
    await manager._settle(7, exit_code=None, error=str(failure.value))
    assert transition.call_args.args[1] == sessions.SessionStatus.FAILED
    assert "SSH host key verification failed" in transition.call_args.kwargs["error"]
    assert "SSH host key verification failed" in event.call_args.args[2]["error"]


def test_unpersisted_first_host_key_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        run_session.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 0, "", "Failed to add the host to the list of known hosts (/logos/ssh_known_hosts)."
        ),
    )
    with pytest.raises(RuntimeError, match="SSH host key could not be persisted"):
        run_session.run(["git", "ls-remote", "git@git.example.org:team/repo.git"])
