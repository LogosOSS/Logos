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
        "LOGOS_VERIFICATION_TREE",
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
    monkeypatch.setattr(verify_checkout, "candidate_tree", lambda _: "a" * 40)
    assert verify_checkout.main([]) == 1
    assert visited == ["logos-webservice", "logos-ui"]
    captured = capsys.readouterr()
    assert "FAILED logos-webservice" in captured.err
    assert "PASSED logos-ui" in captured.out


def test_unrelated_changes_have_explicit_no_compile_selection(monkeypatch, capsys):
    monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", '["logos-agent/app/sessions.py"]')
    monkeypatch.setattr(verify_checkout, "candidate_tree", lambda _: "a" * 40)
    assert verify_checkout.main([]) == 0
    assert "no compile check selected" in capsys.readouterr().out


def _inputs(tmp_path, service):
    checkout = tmp_path / "repo"
    source = checkout / service
    source.mkdir(parents=True)
    (source / "source.txt").write_text("source to verify")
    (source / "target").mkdir()
    (source / "target" / "stale.class").write_text("stale")
    (checkout / ".gitignore").write_text("target/\nnode_modules/\n.mvn/\n")
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "Verification test")
    _git(checkout, "config", "user.email", "verification@example.invalid")
    _commit(checkout)
    cache = tmp_path / "cache"
    (cache / "maven-repository").mkdir(parents=True)
    (cache / "npm-cache").mkdir()
    return checkout, source, cache


def _git(checkout, *args):
    return subprocess.run(
        ["git", "-C", str(checkout), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(checkout):
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "--quiet", "-m", "Candidate")
    return _git(checkout, "rev-parse", "HEAD^{tree}")


def test_maven_compiles_tests_offline_in_private_copy(tmp_path, monkeypatch):
    checkout, source, cache = _inputs(tmp_path, "logos-webservice")
    (source / "src" / "target").mkdir(parents=True)
    (source / "src" / "target" / "Target.java").write_text("class Target {}")
    _git(checkout, "add", "--force", "logos-webservice/src/target/Target.java")
    tree = _commit(checkout)
    calls = []
    monkeypatch.setenv("MAVEN_OPTS", "-Dpoisoned=true")
    monkeypatch.setenv("GIT_ASKPASS", "/workspace/planted-helper")

    def run(command, cwd, env):
        calls.append(command)
        assert cwd != source
        assert (cwd / "source.txt").read_text() == "source to verify"
        assert not (cwd / "target").exists()
        assert (cwd / "src" / "target" / "Target.java").is_file()
        assert env["MAVEN_OPTS"] == f"-Duser.home={env['HOME']}"
        assert "GIT_ASKPASS" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        assert Path(env["HOME"]).is_dir()
        (cwd / "source.txt").write_text("plugin changed the copy")

    monkeypatch.setattr(verify_checkout, "_run", run)
    verify_checkout.verify_service("logos-webservice", checkout, cache, tree)
    assert calls[0][0] == "mvn"
    assert "--offline" in calls[0]
    assert calls[0][-1] == "test-compile"
    assert (source / "source.txt").read_text() == "source to verify"


def test_ui_reinstalls_locked_dependencies_then_builds_and_tests(tmp_path, monkeypatch):
    checkout, source, cache = _inputs(tmp_path, "logos-ui")
    (source / "node_modules").mkdir()
    (source / "node_modules" / "stale.js").write_text("stale")
    calls = []

    def run(command, cwd, env):
        assert not (cwd / "node_modules" / "stale.js").exists()
        calls.append(command)

    monkeypatch.setattr(verify_checkout, "_run", run)
    verify_checkout.verify_service("logos-ui", checkout, cache, _git(checkout, "rev-parse", "HEAD^{tree}"))
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
    _git(tmp_path, "init", "--quiet")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "logos-ui").symlink_to(elsewhere, target_is_directory=True)
    _git(tmp_path, "add", "logos-ui")
    tree = _git(tmp_path, "write-tree")
    with pytest.raises(RuntimeError, match="symlink leaves"):
        verify_checkout.verify_service("logos-ui", tmp_path, tmp_path, tree)


def test_actual_failed_process_propagates(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        verify_checkout._run([sys.executable, "-c", "raise SystemExit(2)"], tmp_path, dict(os.environ))


def test_ignored_maven_config_and_working_edits_cannot_change_verified_candidate(tmp_path, monkeypatch):
    checkout, source, cache = _inputs(tmp_path, "logos-webservice")
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    (source / "source.txt").write_text("different working file")
    (source / ".mvn").mkdir()
    (source / ".mvn" / "maven.config").write_text("-Dmaven.main.skip=true\n-Dmaven.test.skip=true\n")
    original_index = (checkout / ".git" / "index").read_bytes()
    checked = []

    def compile_candidate(command, cwd, env):
        assert not (cwd / ".mvn" / "maven.config").exists()
        assert (cwd / "source.txt").read_text() == "source to verify"
        checked.append(cwd)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setenv("LOGOS_SESSION_PHASE", "verify")
    monkeypatch.setenv("LOGOS_VERIFICATION_TREE", tree)
    monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", '["logos-webservice/source.txt"]')
    monkeypatch.setattr(verify_checkout, "CHECKOUT", checkout)
    monkeypatch.setattr(verify_checkout, "CACHE", cache)
    monkeypatch.setattr(verify_checkout, "_run", compile_candidate)
    assert verify_checkout.main([]) == 1
    assert len(checked) == 1
    assert (checkout / ".git" / "index").read_bytes() == original_index


def test_export_ignore_does_not_omit_tracked_sources(tmp_path):
    checkout, _, _ = _inputs(tmp_path, "logos-webservice")
    (checkout / ".gitattributes").write_text("logos-webservice/source.txt export-ignore\n")
    tree = _commit(checkout)
    scratch = tmp_path / "export"
    scratch.mkdir()
    destination = scratch / "repo"
    verify_checkout.export_candidate(checkout, destination, tree, scratch)
    assert (destination / "logos-webservice" / "source.txt").read_text() == "source to verify"


def test_uncommitted_attributes_cannot_transform_exported_sources(tmp_path):
    checkout, _, _ = _inputs(tmp_path, "logos-webservice")
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    (checkout / ".gitattributes").write_text("* working-tree-encoding=UTF-16LE\n")
    scratch = tmp_path / "export"
    scratch.mkdir()
    destination = scratch / "repo"
    verify_checkout.export_candidate(checkout, destination, tree, scratch)
    assert (destination / "logos-webservice" / "source.txt").read_text() == "source to verify"


@pytest.mark.parametrize("tree", [None, "HEAD", "-a", "12345", "f" * 40, "a" * 64])
def test_authoritative_check_requires_an_existing_tree(tmp_path, monkeypatch, tree):
    checkout, _, _ = _inputs(tmp_path, "logos-ui")
    monkeypatch.setenv("LOGOS_SESSION_PHASE", "verify")
    monkeypatch.setenv("LOGOS_VERIFICATION_PATHS", '["logos-ui/source.txt"]')
    if tree is not None:
        monkeypatch.setenv("LOGOS_VERIFICATION_TREE", tree)
    monkeypatch.setattr(verify_checkout, "CHECKOUT", checkout)
    monkeypatch.setattr(verify_checkout, "verify_service", lambda *args: pytest.fail("invalid tree reached build"))
    assert verify_checkout.main([]) == 1


def test_commit_object_is_not_accepted_as_a_tree(tmp_path, monkeypatch):
    checkout, _, _ = _inputs(tmp_path, "logos-ui")
    monkeypatch.setenv("LOGOS_SESSION_PHASE", "verify")
    monkeypatch.setenv("LOGOS_VERIFICATION_TREE", _git(checkout, "rev-parse", "HEAD"))
    with pytest.raises(ValueError, match="tree object"):
        verify_checkout.candidate_tree(checkout)


def test_manual_candidate_includes_unstaged_edits_and_new_files_without_changing_index(tmp_path):
    checkout, source, _ = _inputs(tmp_path, "logos-ui")
    (source / "source.txt").write_text("manual edit")
    (source / "new.ts").write_text("export const value = 1;")
    original_index = (checkout / ".git" / "index").read_bytes()
    tree = verify_checkout.candidate_tree(checkout)
    assert _git(checkout, "show", f"{tree}:logos-ui/source.txt") == "manual edit"
    assert _git(checkout, "show", f"{tree}:logos-ui/new.ts") == "export const value = 1;"
    assert (checkout / ".git" / "index").read_bytes() == original_index


def test_export_rejects_symlink_back_into_ignored_working_files(tmp_path):
    checkout, source, cache = _inputs(tmp_path, "logos-webservice")
    (source / "external").symlink_to(source / "target", target_is_directory=True)
    tree = _commit(checkout)
    with pytest.raises(RuntimeError, match="symlink leaves the exported tree"):
        verify_checkout.verify_service("logos-webservice", checkout, cache, tree)


def test_export_keeps_internal_symlinks(tmp_path):
    checkout, source, _ = _inputs(tmp_path, "logos-ui")
    (source / "alias.txt").symlink_to("source.txt")
    tree = _commit(checkout)
    scratch = tmp_path / "export"
    scratch.mkdir()
    destination = scratch / "repo"
    verify_checkout.export_candidate(checkout, destination, tree, scratch)
    assert (destination / "logos-ui" / "alias.txt").read_text() == "source to verify"


def test_git_export_supports_worktree_git_files(tmp_path):
    checkout, _, _ = _inputs(tmp_path, "logos-ui")
    linked = tmp_path / "linked"
    _git(checkout, "worktree", "add", "--detach", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    tree = _git(linked, "rev-parse", "HEAD^{tree}")
    scratch = tmp_path / "export"
    scratch.mkdir()
    destination = scratch / "repo"
    verify_checkout.export_candidate(linked, destination, tree, scratch)
    assert (destination / "logos-ui" / "source.txt").read_text() == "source to verify"


def test_checkout_symlink_is_rejected(tmp_path):
    checkout, _, _ = _inputs(tmp_path, "logos-ui")
    link = tmp_path / "linked"
    link.symlink_to(checkout, target_is_directory=True)
    with pytest.raises(RuntimeError, match="checkout must be a directory"):
        verify_checkout.candidate_tree(link)
