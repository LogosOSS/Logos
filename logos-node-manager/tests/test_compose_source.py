"""The compose file of a tag, taken out of its image: what is asked of Docker, and what is never done."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from conftest import FakeEngine

from logos_node_manager.compose_source import BUNDLE_LABEL, ComposeSource
from logos_node_manager.errors import ComposeError
from logos_node_manager.state import Deployment

# What a registry answers through Docker for a tag that does not exist: the classic image store, and the containerd
# image store (the default of Docker Desktop, seen in the smoke test).
MISSING_TAG = [
    "Error response from daemon: manifest for harbor.example/logos/logos-node-manager:v9 not found: manifest unknown",
    'Error response from daemon: failed to resolve reference "harbor.example/logos/logos-node-manager:v9": '
    "harbor.example/logos/logos-node-manager:v9: not found",
]


def write_env(settings, *lines: str) -> None:
    settings.env_file.parent.mkdir(parents=True, exist_ok=True)
    settings.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def deployment(tag: str = "v2", deployment_id: str = "run-1") -> Deployment:
    return Deployment(id=deployment_id, tag=tag, requested_at="2026-01-01T00:00:00")


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def source(settings, engine, store) -> ComposeSource:
    write_env(settings, "REGISTRY=harbor.example/logos")
    return ComposeSource(settings, engine, store.compose_dir)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("NODE_MANAGER_COMPOSE_FROM_TAG=true", True),
        ('NODE_MANAGER_COMPOSE_FROM_TAG="1"', True),
        ("NODE_MANAGER_COMPOSE_FROM_TAG=false", False),
        ("NODE_MANAGER_COMPOSE_FROM_TAG=", False),
        ("SOMETHING_ELSE=true", False),
    ],
)
def test_the_switch_is_read_from_the_env_file_and_is_off_by_default(settings, store, line, expected):
    write_env(settings, line)

    assert ComposeSource(settings, FakeEngine(), store.compose_dir).enabled() is expected


def test_without_an_env_file_the_switch_is_off(settings, store):
    assert ComposeSource(settings, FakeEngine(), store.compose_dir).enabled() is False


def test_the_file_of_the_host_kind_is_taken_out_of_the_image_of_the_tag_without_starting_it(source, engine, store):
    engine.content = "services:\n  logos-workernode: {}\n"

    fetched = source.fetch(deployment())

    assert fetched.image == "harbor.example/logos/logos-node-manager:v2"
    assert fetched.path == store.compose_dir / "run-1" / "worker.yml"
    assert fetched.path.read_text(encoding="utf-8") == engine.content
    assert fetched.sha256 == hashlib.sha256(engine.content.encode()).hexdigest()
    # Pull, look at the label, clear a leftover, make a container that is never started, copy the file, remove it.
    assert engine.verbs() == ["pull", "inspect", "rm", "create", "cp", "rm"]
    assert not any(command[1] in ("run", "start", "exec") for command in engine.calls)
    assert engine.containers == set()
    assert [path.name for path in fetched.path.parent.iterdir()] == ["worker.yml"]


def test_a_core_host_gets_the_file_of_the_core(settings, engine, store):
    write_env(settings, "REGISTRY=harbor.example/logos")
    source = ComposeSource(replace(settings, kind="core"), engine, store.compose_dir)

    fetched = source.fetch(deployment())

    assert fetched.path.name == "core.yaml"
    assert any(command[-2].endswith(":/compose/core.yaml") for command in engine.calls if command[1] == "cp")


def test_the_default_registry_is_used_when_the_env_file_names_none(settings, engine, store):
    assert ComposeSource(settings, engine, store.compose_dir).carrier_reference("v2") == (
        "ghcr.io/logososs/logos-node-manager:v2"
    )


@pytest.mark.parametrize("answer", MISSING_TAG)
def test_a_tag_that_does_not_exist_carries_no_compose_file(source, engine, answer):
    engine.pull_error = answer

    assert source.fetch(deployment("v9")) is None
    assert engine.verbs() == ["pull"]


@pytest.mark.parametrize("labels", [None, {}, {"other": "1"}])
def test_an_image_without_the_label_carries_no_compose_file(source, engine, labels):
    engine.labels = labels

    assert source.fetch(deployment()) is None
    assert "create" not in engine.verbs()


@pytest.mark.parametrize(
    "error",
    [
        "Error response from daemon: unauthorized: authentication required",
        # The containerd image store opens its message like the one for a missing tag. It does not end in "not found".
        'Error response from daemon: failed to resolve reference "harbor.example/logos/logos-node-manager:v2": '
        "pull access denied, repository does not exist or may require authorization: insufficient_scope",
        "Get https://harbor.example/v2/: dial tcp: lookup harbor.example: no such host",
        # A missing program says "not found" too. That is no sign that the tag does not exist.
        "docker: command not found",
    ],
)
def test_any_other_pull_error_is_an_error_and_never_a_reason_to_use_another_file(source, engine, error):
    engine.pull_error = error

    with pytest.raises(ComposeError, match="could not be downloaded"):
        source.fetch(deployment())


def test_a_layout_this_manager_does_not_know_is_an_error_and_says_what_to_do(source, engine):
    engine.labels = {BUNDLE_LABEL: "2"}

    with pytest.raises(ComposeError, match="layout 2.*Update the node manager"):
        source.fetch(deployment())


@pytest.mark.parametrize("content", ["", "x" * (1024 * 1024 + 1)])
def test_an_empty_or_far_too_large_file_is_refused(source, engine, content):
    engine.content = content

    with pytest.raises(ComposeError, match="empty or too large"):
        source.fetch(deployment())

    assert engine.containers == set()


@pytest.mark.parametrize("failing", ["create_error", "copy_error"])
def test_the_container_is_removed_and_no_file_stays_when_a_step_fails(source, engine, store, failing):
    setattr(engine, failing, "boom")

    with pytest.raises(ComposeError, match="boom"):
        source.fetch(deployment())

    assert engine.containers == set()
    assert list((store.compose_dir / "run-1").iterdir()) == []


def test_a_fetch_that_is_repeated_clears_what_the_first_one_left_and_replaces_the_file(source, engine):
    # An attempt that was cut off left its container behind: a second ``create`` with the name would fail.
    engine.containers.add("nm-compose-run-1")
    first = source.fetch(deployment())
    engine.content = "services:\n  changed: {}\n"

    second = source.fetch(deployment())

    assert second.path == first.path
    assert second.path.read_text(encoding="utf-8") == "services:\n  changed: {}\n"
    assert second.sha256 != first.sha256


def test_the_file_that_ran_last_is_kept_for_the_rollback_and_the_host_file_clears_it(source, engine):
    assert source.current_path() is None
    record = deployment()
    record.compose_file = str(source.fetch(record).path)

    source.remember(record)

    assert source.current_path() is not None
    assert source.current_path().read_text(encoding="utf-8") == engine.content
    # A deployment that ran with the compose file of the host leaves nothing for a rollback to restore.
    source.remember(deployment("v3", "run-2"))
    assert source.current_path() is None


def test_the_file_of_the_manager_itself_is_taken_out_of_a_folder_of_its_own(settings, engine, store):
    write_env(settings, "REGISTRY=harbor.example/logos")
    source = ComposeSource(replace(settings, kind="core"), engine, store.compose_dir)

    own = source.fetch(deployment(), own=True)
    theirs = source.fetch(deployment())

    # A deployment and a self-update with the same id do not share a file or a container.
    assert own.path == store.compose_dir / "self-run-1" / "deployer.yml"
    assert theirs.path == store.compose_dir / "run-1" / "core.yaml"
    copied = [command[-2] for command in engine.calls if command[1] == "cp"]
    assert copied[0].endswith(":/compose/deployer.yml") and copied[1].endswith(":/compose/core.yaml")
    created = [command[command.index("--name") + 1] for command in engine.calls if command[1] == "create"]
    assert created == ["nm-compose-self-run-1", "nm-compose-run-1"]


def test_an_image_without_the_file_of_the_manager_carries_none_for_the_update_but_is_an_error_for_a_deployment(
    source, engine
):
    engine.copy_error = "Error response from daemon: Could not find the file /compose/worker.yml in container x"

    assert source.fetch(deployment(), own=True) is None
    with pytest.raises(ComposeError, match="could not be copied"):
        source.fetch(deployment())
    assert engine.containers == set()
