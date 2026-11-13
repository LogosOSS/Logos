"""The registry login from the .env file: when it runs, where it goes and what never leaks."""

from __future__ import annotations

import logging
import stat

import pytest

from logos_node_manager.registry import RegistryLogin, registry_host
from logos_node_manager.runner import CommandResult

PASSWORD = "s3cret-token"


class FakeRunner:
    """Remembers the commands, and what the variable DOCKER_CONFIG is for the commands that follow."""

    def __init__(self, *, error: str | None = None) -> None:
        self.calls: list[dict] = []
        self.docker_config: str | None = None
        self.error = error

    def run(self, argv, *, timeout_s, env=None, stdin=None):
        self.calls.append({"argv": tuple(argv), "env": dict(env or {}), "stdin": stdin})
        if self.error:
            return CommandResult(tuple(argv), 1, stderr=self.error)
        return CommandResult(tuple(argv), 0, stdout="Login Succeeded")

    def set_env(self, name, value):
        assert name == "DOCKER_CONFIG"
        self.docker_config = value


def write_env(settings, *lines: str) -> None:
    settings.env_file.parent.mkdir(parents=True, exist_ok=True)
    settings.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


LOGIN_LINES = ("REGISTRY=harbor.example/logos", "REGISTRY_USER=robot$logos", f"REGISTRY_PASSWORD={PASSWORD}")


@pytest.mark.parametrize(
    ("registry", "host"),
    [
        ("harbor.example/logos", "harbor.example"),
        ("localhost:5001/dummy", "localhost:5001"),
        ("ghcr.io/logososs", "ghcr.io"),
        ("", "ghcr.io"),
        (None, "ghcr.io"),
    ],
)
def test_the_registry_host_is_the_part_before_the_first_slash(registry, host):
    assert registry_host(registry) == host


def test_without_a_login_in_the_env_file_nothing_runs_and_the_login_of_the_host_stays(settings, tmp_path):
    runner = FakeRunner()
    write_env(settings, "REGISTRY=harbor.example/logos")

    assert RegistryLogin(settings, runner, config_dir=tmp_path / "login").login() is None

    assert runner.calls == []
    assert runner.docker_config is None


def test_the_login_goes_to_the_registry_of_the_env_file_with_the_password_on_stdin_only(settings, tmp_path):
    runner = FakeRunner()
    write_env(settings, *LOGIN_LINES)
    directory = tmp_path / "login"

    assert RegistryLogin(settings, runner, config_dir=directory).login() is None

    (call,) = runner.calls
    assert call["argv"] == ("docker", "login", "harbor.example", "--username", "robot$logos", "--password-stdin")
    assert call["stdin"] == PASSWORD
    assert not any(PASSWORD in part for part in call["argv"])
    # The login is made in a directory of its own, and the commands that follow use it.
    assert call["env"] == {"DOCKER_CONFIG": str(directory)}
    assert runner.docker_config == str(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_failed_login_gives_a_reason_without_the_password_and_leaves_no_login_in_use(settings, tmp_path):
    runner = FakeRunner(error=f"Error response from daemon: unauthorized: wrong password {PASSWORD}")
    write_env(settings, *LOGIN_LINES)

    reason = RegistryLogin(settings, runner, config_dir=tmp_path / "login").login()

    assert reason is not None and reason.startswith("the login to the registry harbor.example failed")
    assert PASSWORD not in reason and "***" in reason
    assert runner.docker_config is None


def test_a_new_password_is_used_at_the_next_login_and_removing_it_ends_the_login(settings, tmp_path):
    runner = FakeRunner()
    login = RegistryLogin(settings, runner, config_dir=tmp_path / "login")
    write_env(settings, *LOGIN_LINES)
    login.login()

    write_env(settings, "REGISTRY=harbor.example/logos", "REGISTRY_USER=robot$logos", "REGISTRY_PASSWORD=another-one")
    login.login()
    assert runner.calls[-1]["stdin"] == "another-one"

    write_env(settings, "REGISTRY=harbor.example/logos")
    assert login.login() is None
    assert len(runner.calls) == 2
    assert runner.docker_config is None


def test_one_of_the_two_values_is_a_typo_that_does_not_stop_a_host_with_a_login_of_its_own(settings, tmp_path, caplog):
    runner = FakeRunner()
    write_env(settings, "REGISTRY=harbor.example/logos", "REGISTRY_USER=robot$logos")
    login = RegistryLogin(settings, runner, config_dir=tmp_path / "login")

    with caplog.at_level(logging.WARNING):
        assert login.login() is None
        assert login.login() is None

    assert runner.calls == []
    assert len([record for record in caplog.records if "must both be set" in record.getMessage()]) == 1


def test_the_line_for_the_log_never_holds_the_password(settings, tmp_path):
    write_env(settings, *LOGIN_LINES)

    line = RegistryLogin(settings, FakeRunner(), config_dir=tmp_path / "login").describe()

    assert "robot$logos" in line and "harbor.example" in line
    assert PASSWORD not in line
