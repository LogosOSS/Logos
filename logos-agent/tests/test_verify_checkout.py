"""Offline compilation cannot borrow credentials or alter the pushed tree."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "workspace"))

import verify_checkout  # noqa: E402


@pytest.fixture(autouse=True)
def credential_free(monkeypatch):
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "LOGOS_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "LOGOS_SESSION_PHASE",
        "LOGOS_VERIFICATION_PATHS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_selects_both_services_for_renamed_or_deleted_paths():
    assert verify_checkout.selected_services(
        [
            "docs/guide.md",
            "logos-ui/src/deleted.ts",
            "logos-webservice/src/test/NewTest.java",
            "logos-ui/package-lock.json",
        ]
    ) == ["logos-webservice", "logos-ui"]
    assert verify_checkout.selected_services(["logos-ui-other/file.ts", "logos-agent/workspace/run_session.py"]) == []


@pytest.mark.parametrize("path", ["../logos-ui/file.ts", "/logos-ui/file.ts", "logos-ui/../file", "", 3, "bad\0path"])
def test_rejects_invalid_changed_paths(path):
    with pytest.raises(ValueError):
        verify_checkout.selected_services([path])


@pytest.mark.parametrize("name", ["GITHUB_TOKEN", "GH_TOKEN", "LOGOS_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_refuses_credentials_before_starting_builds(monkeypatch, name):
    monkeypatch.setenv(name, "must-not-reach-build-code")
    monkeypatch.setattr(verify_checkout, "verify_service", lambda *args: pytest.fail("build must not start"))
    assert verify_checkout.main(["logos-webservice"]) == 1


@pytest.mark.parametrize("phase", ["prepare", "snapshot", "finalize", "ssh-trust"])
def test_refuses_credentialed_helper_phase(monkeypatch, phase):
    monkeypatch.setenv("LOGOS_SESSION_PHASE", phase)
    assert verify_checkout.main(["logos-ui"]) == 1


def test_agent_gateway_placeholder_is_not_a_reusable_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "injected-by-logos-agent-gateway")
    verify_checkout.require_credential_free()


@pytest.mark.parametrize("value", [None, "{}", '"logos-ui"', "invalid", '["../logos-ui"]'])
def test_absent_or_malformed_runner_paths_fail_closed(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", value)
    assert verify_checkout.main([]) == 1


def test_reports_both_checks_when_one_fails(monkeypatch, capsys):
    visited = []

    def verify(service, *args):
        visited.append(service)
        if service == "logos-webservice":
            raise subprocess.CalledProcessError(1, ["mvn", "test-compile"])

    monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", json.dumps(["logos-webservice/pom.xml", "logos-ui/src/main.ts"]))
    monkeypatch.setattr(verify_checkout, "verify_service", verify)
    assert verify_checkout.main([]) == 1
    assert visited == ["logos-webservice", "logos-ui"]
    captured = capsys.readouterr()
    assert "FAILED logos-webservice" in captured.err
    assert "PASSED logos-ui" in captured.out


def test_unrelated_changes_have_explicit_no_compile_selection(monkeypatch, capsys):
    monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", '["logos-agent/app/sessions.py"]')
    assert verify_checkout.main([]) == 0
    assert "no compile check selected" in capsys.readouterr().out


def _inputs(tmp_path, service):
    checkout = tmp_path / "repo"
    source = checkout / service
    source.mkdir(parents=True)
    (source / "source.txt").write_text("source to verify")
    (source / "target").mkdir()
    (source / "target" / "stale.class").write_text("stale")
    cache = tmp_path / "cache"
    (cache / "maven-repository").mkdir(parents=True)
    (cache / "npm-cache").mkdir()
    return checkout, source, cache


def test_maven_compiles_tests_offline_in_private_copy(tmp_path, monkeypatch):
    checkout, source, cache = _inputs(tmp_path, "logos-webservice")
    (source / "src" / "target").mkdir(parents=True)
    (source / "src" / "target" / "Target.java").write_text("class Target {}")
    calls = []
    monkeypatch.setenv("MAVEN_OPTS", "-Dpoisoned=true")
    monkeypatch.setenv("GIT_ASKPASS", "/workspace/planted-helper")

    def run(command, cwd, env, check, timeout):
        calls.append(command)
        assert cwd != source
        assert (cwd / "source.txt").read_text() == "source to verify"
        assert not (cwd / "target").exists()
        assert (cwd / "src" / "target" / "Target.java").is_file()
        assert env["MAVEN_OPTS"] == f"-Duser.home={env['HOME']}"
        assert "GIT_ASKPASS" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert Path(env["HOME"]).is_dir()
        assert check and timeout == verify_checkout.COMMAND_TIMEOUT
        (cwd / "source.txt").write_text("plugin changed the copy")

    monkeypatch.setattr(verify_checkout.subprocess, "run", run)
    verify_checkout.verify_service("logos-webservice", checkout, cache)
    assert calls[0][0] == "mvn"
    assert "--offline" in calls[0]
    assert calls[0][-1] == "test-compile"
    assert (source / "source.txt").read_text() == "source to verify"


def test_ui_reinstalls_locked_dependencies_then_builds_and_tests(tmp_path, monkeypatch):
    checkout, source, cache = _inputs(tmp_path, "logos-ui")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "stale.js").write_text("stale")
    calls = []

    def run(command, cwd, env, check, timeout):
        assert not (cwd / "node_modules" / "stale.js").exists()
        calls.append(command)

    monkeypatch.setattr(verify_checkout.subprocess, "run", run)
    verify_checkout.verify_service("logos-ui", checkout, cache)
    assert calls[0][:3] == ["npm", "ci", "--offline"]
    assert calls[1][-3:] == ["build", "--configuration", "production"]
    assert calls[2][-2:] == ["test", "--watch=false"]


def test_cache_miss_is_a_failure_not_a_skipped_check(tmp_path, monkeypatch):
    checkout, _, _ = _inputs(tmp_path, "logos-ui")
    monkeypatch.setattr(verify_checkout, "CHECKOUT", checkout)
    monkeypatch.setattr(verify_checkout, "CACHE", tmp_path / "missing-cache")
    assert verify_checkout.main(["logos-ui"]) == 1


def test_deleted_service_fails_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(verify_checkout, "CHECKOUT", tmp_path)
    assert verify_checkout.main(["logos-webservice"]) == 1


def test_linked_service_directory_is_rejected(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "logos-ui").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeError, match="linked service"):
        verify_checkout.verify_service("logos-ui", tmp_path, tmp_path)


def test_actual_failed_process_propagates(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        verify_checkout._run([sys.executable, "-c", "raise SystemExit(2)"], tmp_path, dict(os.environ))
