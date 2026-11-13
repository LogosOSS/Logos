"""Settings from the environment."""

from __future__ import annotations

from pathlib import Path

import pytest

from logos_node_manager.config import Settings, compose_project_name


def test_the_defaults_match_the_layout_of_a_worker_host():
    settings = Settings.from_env({})

    assert settings.project_dir == Path("/opt/logos-workernode")
    assert settings.compose_file == Path("/opt/logos-workernode/docker-compose.yml")
    assert settings.env_file == Path("/opt/logos-workernode/.env")
    assert settings.state_dir == Path("/opt/logos-workernode/node-manager")
    assert settings.project_name == "logos-workernode"
    assert settings.service == "logos-workernode"
    assert settings.container == "logos-workernode"
    assert settings.pull_timeout_s == 1800
    assert settings.stability_window_s == 60
    assert settings.drain_enabled is False


def test_paths_follow_the_project_directory():
    settings = Settings.from_env({"NODE_MANAGER_PROJECT_DIR": "/srv/Worker One"})

    assert settings.compose_file == Path("/srv/Worker One/docker-compose.yml")
    assert settings.project_name == "workerone"


@pytest.mark.parametrize(
    ("directory", "expected"), [("/opt/logos-workernode", "logos-workernode"), ("/x/My_App", "my_app")]
)
def test_the_project_name_follows_the_rules_compose_applies(directory, expected):
    assert compose_project_name(Path(directory)) == expected


def test_values_can_be_overridden():
    settings = Settings.from_env(
        {
            "NODE_MANAGER_SERVICE": "worker",
            "NODE_MANAGER_CONTAINER": "nm-worker",
            "NODE_MANAGER_VERIFY_TIMEOUT_S": "90",
            "NODE_MANAGER_DRAIN_ENABLED": "true",
            "NODE_MANAGER_LOG_LEVEL": "debug",
        }
    )

    assert (settings.service, settings.container) == ("worker", "nm-worker")
    assert settings.verify_timeout_s == 90
    assert settings.drain_enabled is True
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize("value", ["", "abc", "0", "-5"])
def test_a_bad_timeout_falls_back_to_the_default_instead_of_switching_it_off(value):
    assert Settings.from_env({"NODE_MANAGER_PULL_TIMEOUT_S": value}).pull_timeout_s == 1800


def test_the_stability_window_may_be_switched_off():
    assert Settings.from_env({"NODE_MANAGER_STABILITY_WINDOW_S": "0"}).stability_window_s == 0


def test_the_drain_has_no_orchestrator_and_generous_limits_by_default():
    settings = Settings.from_env({})

    assert settings.orchestrator_url == ""
    assert settings.node_key == ""
    assert settings.drain_timeout_s == 900
    assert settings.turn_timeout_s == 7200
    assert settings.orchestrator_retry_window_s == 120
    assert settings.orchestrator_timeout_s == 15
    assert settings.reconnect_timeout_s == 300


def test_the_orchestrator_is_read_from_the_environment():
    settings = Settings.from_env(
        {
            "NODE_MANAGER_ORCHESTRATOR_URL": "https://logos.example/",
            "NODE_MANAGER_NODE_KEY": " the-key ",
            "NODE_MANAGER_DRAIN_TIMEOUT_S": "300",
            "NODE_MANAGER_TURN_TIMEOUT_S": "60",
        }
    )

    assert settings.orchestrator_url == "https://logos.example"
    assert settings.node_key == "the-key"
    assert settings.drain_timeout_s == 300
    assert settings.turn_timeout_s == 60


@pytest.mark.parametrize(
    "name", ["DRAIN_TIMEOUT_S", "TURN_TIMEOUT_S", "RECONNECT_TIMEOUT_S", "ORCHESTRATOR_RETRY_WINDOW_S"]
)
@pytest.mark.parametrize("value", ["", "abc", "0", "-5"])
def test_a_bad_drain_limit_falls_back_to_the_default_instead_of_switching_it_off(name, value):
    defaults = Settings.from_env({})
    settings = Settings.from_env({f"NODE_MANAGER_{name}": value})

    assert settings == defaults


def test_the_node_key_is_not_part_of_the_text_form_of_the_settings():
    settings = Settings.from_env({"NODE_MANAGER_NODE_KEY": "the-secret-node-key"})

    assert "the-secret-node-key" not in repr(settings)
    assert "the-secret-node-key" not in str(settings)


def test_the_service_of_the_manager_itself_follows_the_kind_of_the_host():
    worker = Settings.from_env({})
    core = Settings.from_env({"NODE_MANAGER_KIND": "core"})

    # A worker host has the manager next to the worker, in the same compose file.
    assert worker.self_project_name == worker.project_name == "logos-workernode"
    assert worker.self_compose_file == worker.compose_file
    assert (worker.self_service, worker.self_container) == ("logos-node-manager", "logos-node-manager")
    # A core host has it in a project of its own, so that a deployment of Logos never replaces it.
    assert (core.self_project_name, core.self_service, core.self_container) == ("logos-deployer",) * 3
    assert core.self_compose_file == Path("/opt/logos/docker-compose.deployer.yml")
    assert worker.self_verify_timeout_s == 180


def test_a_worker_project_with_another_name_is_also_the_project_of_its_manager():
    assert Settings.from_env({"NODE_MANAGER_PROJECT_NAME": "gpu-one"}).self_project_name == "gpu-one"


def test_the_service_of_the_manager_itself_can_be_named():
    settings = Settings.from_env(
        {
            "NODE_MANAGER_SELF_PROJECT_NAME": "p",
            "NODE_MANAGER_SELF_COMPOSE_FILE": "/x/y.yml",
            "NODE_MANAGER_SELF_SERVICE": "s",
            "NODE_MANAGER_SELF_CONTAINER": "c",
            "NODE_MANAGER_SELF_VERIFY_TIMEOUT_S": "42",
        }
    )

    assert (settings.self_project_name, settings.self_service, settings.self_container) == ("p", "s", "c")
    assert settings.self_compose_file == Path("/x/y.yml") and settings.self_verify_timeout_s == 42
