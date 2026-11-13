"""The start of a deployment: the registry login and the choice of the compose file, before anything is pulled."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import NEW, OLD, REPO, UNHEALTHY, FakeEngine, queued

from logos_node_manager.compose_source import ComposeSource
from logos_node_manager.errors import DockerError
from logos_node_manager.state import FailureKind, Outcome, Phase

LOGIN_FAILED = "the login to the registry harbor.example failed: unauthorized"
# The classic image store and the containerd image store (Docker Desktop) say it differently.
MISSING_TAG = [
    "Error response from daemon: manifest for harbor.example/logos/logos-node-manager:v2 not found: manifest unknown",
    'Error response from daemon: failed to resolve reference "harbor.example/logos/logos-node-manager:v2": '
    "harbor.example/logos/logos-node-manager:v2: not found",
]


class StubRegistry:
    """Stands for the registry login: counts the logins and fails with the given reason, if there is one."""

    def __init__(self, problem: str | None = None) -> None:
        self.problem = problem
        self.logins = 0

    def login(self) -> str | None:
        self.logins += 1
        return self.problem


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def source(settings, engine, store) -> ComposeSource:
    write_env(settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=true")
    return ComposeSource(settings, engine, store.compose_dir)


def write_env(settings, *lines: str) -> None:
    settings.env_file.parent.mkdir(parents=True, exist_ok=True)
    settings.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pulled(docker) -> bool:
    return any(call[0] == "pull" for call in docker.calls)


# ---------------------------------------------------------------------- The registry login


def test_a_failed_registry_login_ends_the_deployment_before_anything_is_touched(make_deployer, docker, store):
    record = make_deployer(registry=StubRegistry(LOGIN_FAILED)).process(queued(store, "v2"))

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.PULL_FAILED
    assert record.message == f"{LOGIN_FAILED}. Nothing on the host was touched."
    assert docker.calls == []
    assert docker.container_info.id == "c-old"


def test_the_login_is_made_once_at_the_start_and_the_deployment_goes_on(make_deployer, store):
    registry = StubRegistry()

    record = make_deployer(registry=registry).process(queued(store, "v2"))

    assert registry.logins == 1
    assert record.outcome is Outcome.SUCCEEDED


# ---------------------------------------------------------------------- The compose file


def test_the_compose_file_of_the_tag_is_used_for_the_whole_deployment(make_deployer, docker, store, source, engine):
    engine.content = "services:\n  logos-workernode: {}\n"

    record = make_deployer(compose=source).process(queued(store, "v2"))

    assert record.outcome is Outcome.SUCCEEDED
    path = store.compose_dir / "run-1" / "worker.yml"
    assert record.compose_file == str(path)
    assert record.compose_sha256 == hashlib.sha256(engine.content.encode()).hexdigest()
    assert record.compose_settled
    assert docker.compose_path == path
    # The file is chosen and checked before the first step that needs it.
    assert docker.calls.index(("validate", "v2")) < docker.calls.index(("pull", "v2"))
    assert any(
        f"using the compose file worker.yml of tag v2 (sha256 {record.compose_sha256[:12]})" == e["note"]
        for e in record.events
    )
    # A deployment that ended well leaves its file for the rollback of the next one.
    assert source.current_path().read_text(encoding="utf-8") == engine.content


def test_with_the_switch_off_the_host_keeps_its_own_file_and_the_image_is_not_asked(
    make_deployer, docker, store, settings, engine
):
    write_env(settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=false")
    off = ComposeSource(settings, engine, store.compose_dir)

    record = make_deployer(compose=off).process(queued(store, "v2"))

    assert record.outcome is Outcome.SUCCEEDED
    assert record.compose_file == "" and record.compose_settled
    assert engine.calls == []
    assert docker.compose_path is None


@pytest.mark.parametrize("answer", MISSING_TAG)
def test_a_tag_without_compose_files_uses_the_file_of_the_host_and_says_so(
    make_deployer, docker, store, source, engine, answer
):
    engine.pull_error = answer

    record = make_deployer(compose=source).process(queued(store, "v2"))

    assert record.outcome is Outcome.SUCCEEDED
    assert record.compose_file == "" and record.compose_settled
    assert docker.compose_path is None
    assert any("carries no compose file: using the compose file of the host" in e["note"] for e in record.events)
    assert source.current_path() is None


def test_an_image_that_cannot_be_had_ends_the_deployment_before_anything_is_touched(
    make_deployer, docker, store, source, engine
):
    engine.pull_error = "Error response from daemon: unauthorized: authentication required"

    record = make_deployer(compose=source).process(queued(store, "v2"))

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.PULL_FAILED
    assert "could not be downloaded" in record.message and record.message.endswith("Nothing on the host was touched.")
    assert not pulled(docker)
    assert docker.container_info.id == "c-old"


def test_a_compose_file_that_does_not_work_with_the_env_file_ends_the_deployment_before_anything_is_touched(
    make_deployer, docker, store, source
):
    docker.validate_error = "required variable LOGOS_DB_PASSWORD is missing a value"

    record = make_deployer(compose=source).process(queued(store, "v2"))

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.PULL_FAILED
    assert "does not work with the .env file of this host" in record.message
    assert "LOGOS_DB_PASSWORD" in record.message
    assert not pulled(docker) and not any(call[0] == "up" for call in docker.calls)
    assert docker.compose_path is None
    assert record.compose_file == ""


def test_a_deployment_that_goes_on_after_a_restart_keeps_the_file_it_began_with(
    make_deployer, docker, store, source, engine
):
    kept = store.compose_dir / "run-1" / "worker.yml"
    kept.parent.mkdir(parents=True)
    kept.write_text("services: {}\n", encoding="utf-8")
    record = queued(store, "v2")
    record.phase = Phase.PULLING
    record.compose_file, record.compose_settled = str(kept), True

    make_deployer(compose=source).process(record)

    # The tag may have moved on since. The deployment still ends with the settings it started with.
    assert engine.calls == []
    assert docker.compose_path == Path(kept)


def test_a_compose_file_that_the_record_names_but_that_is_gone_is_an_error_not_a_silent_fallback(
    make_deployer, store, source
):
    record = queued(store, "v2")
    record.phase = Phase.SWAPPING
    record.compose_file, record.compose_settled = str(store.compose_dir / "run-1" / "worker.yml"), True

    with pytest.raises(DockerError, match="compose file of this deployment is missing"):
        make_deployer(compose=source).process(record)


def test_a_rollback_restores_the_previous_image_together_with_the_previous_compose_file(
    make_deployer, docker, store, source, engine
):
    deployer = make_deployer(compose=source)
    # The first deployment changes nothing and ends well, so its compose file is the one that ran last.
    engine.content = "services: previous\n"
    docker.registry[f"{REPO}:latest"] = OLD
    assert deployer.process(queued(store, "latest", "run-1")).outcome is Outcome.UNCHANGED
    # The second one brings a new compose file and an image that does not become healthy.
    engine.content = "services: new\n"
    docker.new_container_timeline = list(UNHEALTHY)
    docker.registry[f"{REPO}:v2"] = NEW

    record = deployer.process(queued(store, "v2", "run-2"))

    assert record.outcome is Outcome.ROLLED_BACK
    assert Path(record.compose_file).read_text(encoding="utf-8") == "services: new\n"
    restoring = docker.calls.index(("up", "latest", True, True))
    last_file_before = [call[1] for call in docker.calls[:restoring] if call[0] == "use_compose"][-1]
    assert last_file_before.read_text(encoding="utf-8") == "services: previous\n"
    # The rollback did not end well for the new file: the old one is still the one that ran last.
    assert source.current_path().read_text(encoding="utf-8") == "services: previous\n"
