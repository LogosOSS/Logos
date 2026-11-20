"""The command lines of a core host: the whole compose project, its replicas, its containers and its logs."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_docker_ops import FakeRunner, failed, inspect_json, ok

from logos_node_manager.docker_ops import ONE_OFF_LABEL, PROJECT_LABEL, SERVICE_LABEL, ProjectDocker
from logos_node_manager.errors import DockerError


def project_with(settings, *results, **changes) -> tuple[ProjectDocker, FakeRunner]:
    runner = FakeRunner(*results)
    return ProjectDocker(replace(settings, **changes), runner), runner


def test_pull_project_pulls_every_service_with_the_tag_in_the_environment(settings):
    docker, runner = project_with(settings, ok())

    assert docker.pull_project("pr-93").ok
    argv, timeout, env = runner.calls[0]
    assert argv[-1] == "pull"
    assert timeout == settings.pull_timeout_s
    assert "pr-93" in env.values()


def test_up_project_scales_the_service_and_removes_orphans(settings):
    docker, runner = project_with(settings, ok())

    docker.up_project("pr-93", 3)

    argv, timeout, _ = runner.calls[0]
    assert argv[-5:] == ("up", "-d", "--remove-orphans", "--scale", "logos-webservice=3")
    assert timeout == settings.swap_timeout_s


def test_up_project_does_not_scale_when_no_service_is_named(settings):
    docker, runner = project_with(settings, ok(), scale_service="")

    docker.up_project("pr-93", 3)

    assert "--scale" not in runner.calls[0][0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, 1),
        ("LOGOS_WEBSERVICE_REPLICAS=\n", 1),
        ("LOGOS_WEBSERVICE_REPLICAS=3\n", 3),
        ("LOGOS_WEBSERVICE_REPLICAS=many\n", 1),
        ("LOGOS_WEBSERVICE_REPLICAS=0\n", 1),
    ],
)
def test_replicas_come_from_the_env_file_and_default_to_one(settings, tmp_path, text, expected):
    env_file = tmp_path / ".env"
    if text is not None:
        env_file.write_text(text, encoding="utf-8")
    docker, _ = project_with(settings, env_file=env_file)

    assert docker.replicas() == expected


def test_services_are_the_lines_of_compose_config(settings):
    docker, runner = project_with(settings, ok("logos-webservice\n\nlogos-ui\n"))

    assert docker.services() == ["logos-webservice", "logos-ui"]
    assert runner.calls[0][0][-2:] == ("config", "--services")


def test_a_failing_compose_config_is_an_error(settings):
    docker, _ = project_with(settings, failed("no such file"))

    with pytest.raises(DockerError, match="compose config failed"):
        docker.services()


def container(service: str, **labels) -> dict:
    raw = json.loads(inspect_json())[0]
    raw["Config"]["Labels"] = {SERVICE_LABEL: service, **labels}
    return raw


def test_the_containers_of_the_project_skip_one_off_containers(settings):
    inspected = json.dumps([container("logos-webservice"), container("migrate", **{ONE_OFF_LABEL: "True"})])
    docker, runner = project_with(settings, ok("id1\nid2\n"), ok(inspected))

    found = docker.project_containers()

    assert [item.service for item in found] == ["logos-webservice"]
    assert f"label={PROJECT_LABEL}={settings.project_name}" in runner.calls[0][0]


def test_a_project_without_containers_is_not_inspected(settings):
    docker, runner = project_with(settings, ok("\n"))

    assert docker.project_containers() == []
    assert len(runner.calls) == 1


def test_a_failing_listing_is_an_error(settings):
    docker, _ = project_with(settings, failed("daemon down"))

    with pytest.raises(DockerError, match="docker ps failed"):
        docker.project_containers()


def test_a_failing_inspect_is_an_error(settings):
    docker, _ = project_with(settings, ok("id1\n"), failed("gone"))

    with pytest.raises(DockerError, match="docker inspect failed"):
        docker.project_containers()


def test_unexpected_inspect_output_is_an_error(settings):
    docker, _ = project_with(settings, ok("id1\n"), ok(json.dumps(["not a container"])))

    with pytest.raises(DockerError, match="unexpected output"):
        docker.project_containers()


def test_the_log_of_services_combines_both_streams_and_is_empty_without_services(settings):
    docker, runner = project_with(settings, ok("out\n"))

    assert docker.service_logs([]) == ""
    assert runner.calls == []
    assert docker.service_logs(["logos-webservice"], lines=5) == "out"
    assert runner.calls[0][0][-3:] == ("--tail", "5", "logos-webservice")


def test_prune_and_single_pull_use_plain_docker(settings):
    docker, runner = project_with(settings, ok(), ok())

    docker.prune_images()
    docker.pull_image("registry.example/worker:v2")

    assert runner.calls[0][0] == ("docker", "image", "prune", "-af")
    assert runner.calls[1][0] == ("docker", "pull", "registry.example/worker:v2")
