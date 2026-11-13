"""The manager replaces itself: the request, the preparation, the hand-over to the helper, and the watch over it."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from conftest import NEW, OLD, REPO, FakeEngine
from self_update_support import FakeHelperEngine, StubRegistry, Wall, ended, handed_over, request

from logos_node_manager.compose_source import ComposeSource
from logos_node_manager.errors import DockerError
from logos_node_manager.self_update import (
    HELPER_MAX_S,
    HELPER_NAME,
    Failure,
    Helper,
    SelfUpdate,
    SelfUpdater,
    UpdatePhase,
)
from logos_node_manager.state import Deployment, Outcome, Phase

LOGIN_FAILED = "the login to the registry harbor.example failed: unauthorized"


def write_env(settings, *lines: str) -> None:
    settings.env_file.parent.mkdir(parents=True, exist_ok=True)
    settings.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def wall() -> Wall:
    return Wall()


@pytest.fixture
def engine() -> FakeHelperEngine:
    return FakeHelperEngine()


@pytest.fixture
def make_updater(own_settings, own_docker, su_store, engine, wall):
    """Build the manager's part of the self-update. The manager runs in the container ``c-old``."""

    def build(**overrides) -> SelfUpdater:
        options = {"hostname": lambda: "c-old", "environ": {}, "now": wall}
        options.update(overrides)
        return SelfUpdater(options.pop("settings", own_settings), own_docker, su_store, engine, **options)

    return build


@pytest.fixture
def updater(make_updater) -> SelfUpdater:
    return make_updater()


def work(updater: SelfUpdater, *, pending: bool = False) -> bool:
    return updater.step(deployments_pending=pending)


# ---------------------------------------------------------------------- Asking for an update


@pytest.mark.parametrize(
    ("outcome", "queued"),
    [(Outcome.SUCCEEDED, True), (Outcome.UNCHANGED, True), (Outcome.FAILED, False), (Outcome.ROLLED_BACK, False)],
)
def test_only_a_deployment_that_ended_well_is_followed_by_an_update(updater, su_store, own_settings, outcome, queued):
    write_env(own_settings, "NODE_MANAGER_SELF_UPDATE=true")
    deployment = Deployment(id="run-1", tag="v2", requested_at="x", phase=Phase.DONE, outcome=outcome)

    updater.after_deployment(deployment)

    assert (su_store.load("run-1") is not None) is queued


def test_with_the_switch_off_a_deployment_is_not_followed_by_an_update(updater, su_store, own_settings):
    write_env(own_settings, "NODE_MANAGER_SELF_UPDATE=false")

    updater.after_deployment(
        Deployment(id="run-1", tag="v2", requested_at="x", phase=Phase.DONE, outcome=Outcome.SUCCEEDED)
    )

    assert su_store.all() == []
    assert "only on request" in updater.describe()


def test_a_deployment_is_followed_by_one_request_only(updater, su_store, own_settings):
    write_env(own_settings, "NODE_MANAGER_SELF_UPDATE=true")
    deployment = Deployment(id="run-1", tag="v2", requested_at="x", phase=Phase.DONE, outcome=Outcome.SUCCEEDED)

    updater.after_deployment(deployment)
    first = su_store.load("run-1")
    first.phase = UpdatePhase.DONE
    first.outcome = Outcome.FAILED
    su_store.save(first)
    updater.after_deployment(deployment)

    # The ending of the first request stays: a request that failed is not made again for the same deployment.
    assert [record.id for record in su_store.all()] == ["run-1"]
    assert su_store.load("run-1").outcome is Outcome.FAILED
    assert "after each deployment" in updater.describe()


# ---------------------------------------------------------------------- Preparing the hand-over


def test_a_new_image_is_handed_to_a_helper_that_runs_the_image_that_runs_now(updater, su_store, engine, own_docker):
    request(su_store)

    assert work(updater) is True

    record = su_store.load("u1")
    assert record.phase is UpdatePhase.HANDED_OVER
    assert (record.previous_image_id, record.target_image_id) == (OLD, NEW)
    assert (record.previous_container_id, record.target_image_ref) == ("c-old", f"{REPO}:v2")
    command = engine.started()
    assert command[command.index("--volumes-from") + 1] == "c-old"
    assert command[-3:] == (OLD, "self-update-run", "u1")
    # Only the image of the new manager was fetched. The manager itself is not touched before the helper has the swap.
    assert ("pull", "v2") in own_docker.calls
    assert not any(call[0] == "up" for call in own_docker.calls)


def test_the_helper_gets_the_volumes_and_the_settings_of_the_manager_but_no_secret_and_no_network(own_settings, engine):
    environ = {
        "NODE_MANAGER_PROJECT_DIR": "/opt/x",
        "NODE_MANAGER_STATE_DIR": "/opt/x/state",
        "NODE_MANAGER_NODE_KEY": "the-worker-key",
        "NODE_MANAGER_DEPLOYER_KEY": "the-deployer-key",
        "NODE_MANAGER_SOME_TOKEN": "a-token",
        "PATH": "/usr/bin",
        "DOCKER_CONFIG": "/somewhere",
    }

    command = Helper(own_settings, engine, environ).command(OLD, "u1", "c-old")

    for flag in ("-d", "--init", "--read-only"):
        assert flag in command
    pairs = {(command[i], command[i + 1]) for i in range(len(command) - 1)}
    assert {
        ("--name", HELPER_NAME),
        ("--network", "none"),
        ("--cap-drop", "ALL"),
        ("--cap-add", "DAC_OVERRIDE"),
        ("--security-opt", "no-new-privileges:true"),
        ("--volumes-from", "c-old"),
    } <= pairs
    # A variable is named, never given a value, so that no value is on a command line.
    assert [command[i + 1] for i, part in enumerate(command) if part == "-e"] == [
        "NODE_MANAGER_PROJECT_DIR",
        "NODE_MANAGER_STATE_DIR",
    ]
    for secret in ("the-worker-key", "the-deployer-key", "a-token"):
        assert secret not in " ".join(command)


def test_a_queued_update_waits_while_a_deployment_waits(updater, su_store, engine, own_docker):
    request(su_store)

    assert work(updater, pending=True) is False

    assert su_store.load("u1").phase is UpdatePhase.QUEUED
    assert engine.calls == [] and own_docker.calls == []


def test_while_the_helper_has_the_swap_no_deployment_may_start(updater, su_store):
    request(su_store)
    work(updater)

    assert work(updater, pending=True) is True
    assert work(updater, pending=False) is True
    assert su_store.load("u1").phase is UpdatePhase.HANDED_OVER


def test_an_update_to_the_image_that_runs_already_changes_nothing(updater, su_store, engine, own_docker):
    own_docker.registry[f"{REPO}:v2"] = OLD
    request(su_store)

    assert work(updater) is False

    record = su_store.load("u1")
    assert record.finished and record.outcome is Outcome.UNCHANGED
    assert "already runs" in record.message
    assert "run" not in engine.verbs()


@pytest.mark.parametrize(
    ("container", "hostname", "message"),
    [
        (None, "c-old", "there is no container logos-node-manager"),
        ({}, "c-other", "runs in the container c-other, not in logos-node-manager"),
        ({"project": "someone-else"}, "c-old", "was not made by Compose as the service logos-node-manager"),
        ({"service": "someone-else"}, "c-old", "was not made by Compose as the service logos-node-manager"),
        ({"project": None, "service": None}, "c-old", "it says nothing and nothing"),
        ({"image_ref": "sha256:" + "a" * 64}, "c-old", "made from an image ID"),
    ],
)
def test_a_manager_that_cannot_replace_itself_ends_the_request_before_anything_is_touched(
    make_updater, su_store, own_docker, engine, container, hostname, message
):
    own_docker.container_info = None if container is None else replace(own_docker.container_info, **container)
    request(su_store)

    assert work(make_updater(hostname=lambda: hostname)) is False

    record = su_store.load("u1")
    assert record.outcome is Outcome.FAILED and record.failure is Failure.NOT_ELIGIBLE
    assert message in record.message and record.message.endswith("Nothing on the host was touched.")
    assert engine.calls == [] and not any(call[0] == "pull" for call in own_docker.calls)


def test_a_failed_login_ends_the_request_before_anything_is_touched(make_updater, su_store, own_docker, engine):
    request(su_store)

    work(make_updater(registry=StubRegistry(LOGIN_FAILED)))

    record = su_store.load("u1")
    assert record.failure is Failure.PULL_FAILED
    assert record.message == f"{LOGIN_FAILED}. Nothing on the host was touched."
    assert own_docker.calls == [] and engine.calls == []


@pytest.mark.parametrize("tag", ["v9", "latest"])
def test_an_image_that_cannot_be_had_ends_the_request_before_the_manager_is_touched(
    updater, su_store, own_docker, engine, tag
):
    own_docker.pull_error = "unauthorized: authentication required" if tag == "latest" else None
    request(su_store, tag)

    work(updater)

    record = su_store.load("u1")
    assert record.outcome is Outcome.FAILED and record.failure is Failure.PULL_FAILED
    assert record.message.endswith("The manager was not touched.")
    assert engine.calls == []


def test_a_login_is_made_once_before_the_pull(make_updater, su_store, own_docker):
    registry = StubRegistry()
    request(su_store)

    work(make_updater(registry=registry))

    assert registry.logins == 1
    assert su_store.load("u1").phase is UpdatePhase.HANDED_OVER


def test_the_compose_file_of_the_tag_is_used_for_the_swap_and_checked_first(
    make_updater, su_store, own_docker, own_settings, store
):
    write_env(own_settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=true")
    files = FakeEngine()
    files.content = "services:\n  logos-node-manager: {}\n"
    request(su_store)

    work(make_updater(compose=ComposeSource(own_settings, files, store.compose_dir)))

    record = su_store.load("u1")
    path = store.compose_dir / "self-u1" / "worker.yml"
    assert record.compose_file == str(path)
    assert record.compose_sha256 == hashlib.sha256(files.content.encode()).hexdigest()
    assert own_docker.compose_path == path
    # The file is chosen and checked before the image is pulled.
    assert own_docker.calls.index(("validate", "v2")) < own_docker.calls.index(("pull", "v2"))
    assert any("using the compose file worker.yml of tag v2" in event["note"] for event in record.events)


def test_a_compose_file_that_does_not_work_with_the_env_file_ends_the_request(
    make_updater, su_store, own_docker, own_settings, store, engine
):
    write_env(own_settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=true")
    own_docker.validate_error = "required variable LOGOS_DB_PASSWORD is missing a value"
    request(su_store)

    work(make_updater(compose=ComposeSource(own_settings, FakeEngine(), store.compose_dir)))

    record = su_store.load("u1")
    assert record.failure is Failure.PULL_FAILED
    assert "does not work with the .env file of this host" in record.message and "LOGOS_DB_PASSWORD" in record.message
    assert own_docker.compose_path is None and record.compose_file == ""
    assert not any(call[0] == "pull" for call in own_docker.calls) and engine.calls == []


def test_an_image_without_a_file_for_the_manager_falls_back_to_the_compose_file_of_the_host(
    make_updater, su_store, own_docker, own_settings, store
):
    write_env(own_settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=true")
    files = FakeEngine()
    files.copy_error = "Error response from daemon: Could not find the file /compose/worker.yml in container x"
    request(su_store)

    work(make_updater(compose=ComposeSource(own_settings, files, store.compose_dir)))

    record = su_store.load("u1")
    assert record.phase is UpdatePhase.HANDED_OVER and record.compose_file == ""
    assert any("carries no compose file for the manager" in event["note"] for event in record.events)


def test_an_image_that_cannot_be_downloaded_for_its_compose_file_ends_the_request(
    make_updater, su_store, own_docker, own_settings, store, engine
):
    write_env(own_settings, "REGISTRY=harbor.example/logos", "NODE_MANAGER_COMPOSE_FROM_TAG=true")
    files = FakeEngine()
    files.pull_error = "Error response from daemon: unauthorized: authentication required"
    request(su_store)

    work(make_updater(compose=ComposeSource(own_settings, files, store.compose_dir)))

    record = su_store.load("u1")
    assert record.failure is Failure.PULL_FAILED
    assert "could not be downloaded" in record.message and record.message.endswith("Nothing on the host was touched.")
    assert not any(call[0] == "pull" for call in own_docker.calls) and engine.calls == []


def test_a_compose_file_of_the_host_that_is_missing_ends_the_request(updater, su_store, own_settings, own_docker):
    own_settings.self_compose_file.unlink()
    request(su_store)

    work(updater)

    record = su_store.load("u1")
    assert record.failure is Failure.NOT_ELIGIBLE
    assert "the compose file of the manager does not exist" in record.message
    assert not any(call[0] == "pull" for call in own_docker.calls)


def test_a_helper_that_cannot_be_started_ends_the_request_and_leaves_the_manager_alone(updater, su_store, engine):
    engine.run_error = "docker: Error response from daemon: no such image"
    request(su_store)

    assert work(updater) is False

    record = su_store.load("u1")
    assert record.outcome is Outcome.FAILED and record.failure is Failure.HELPER_FAILED
    assert "the helper could not be started" in record.message and record.message.endswith("not touched.")


def test_a_helper_that_still_runs_blocks_a_second_one(updater, su_store, engine):
    engine.exists = engine.running = True
    request(su_store)

    work(updater)

    record = su_store.load("u1")
    assert record.failure is Failure.HELPER_FAILED and "another helper is running" in record.message
    assert engine.verbs() == ["inspect"]


def test_what_a_stopped_helper_left_behind_is_removed_before_a_new_one_starts(updater, su_store, engine):
    engine.exists, engine.running = True, False
    request(su_store)

    assert work(updater) is True

    assert engine.verbs() == ["inspect", "rm", "run"]


@pytest.mark.parametrize(
    ("config_files", "expected"),
    [("OWN", "OWN"), ("OWN,OWN", ""), ("/nowhere/docker-compose.yml", ""), (None, "")],
)
def test_the_compose_file_that_made_the_manager_is_kept_for_the_way_back(
    updater, su_store, own_docker, tmp_path, config_files, expected
):
    old = tmp_path / "old.yml"
    old.write_text("services: {}\n", encoding="utf-8")
    config_files = None if config_files is None else config_files.replace("OWN", str(old))
    expected = str(old) if expected == "OWN" else expected
    own_docker.container_info = replace(own_docker.container_info, config_files=config_files)
    request(su_store)

    work(updater)

    assert su_store.load("u1").rollback_compose_file == expected


def test_a_manager_that_fails_again_and_again_gives_the_request_up(updater, su_store, own_docker, monkeypatch):
    def broken(tag):
        raise RuntimeError("boom")

    monkeypatch.setattr(own_docker, "pull", broken)
    request(su_store)

    assert work(updater) is False
    assert su_store.load("u1").attempts == 1 and not su_store.load("u1").finished
    work(updater)
    work(updater)

    record = su_store.load("u1")
    assert record.outcome is Outcome.FAILED and record.failure is Failure.INTERNAL_ERROR
    assert "the manager failed 3 times: boom" in record.message


# ---------------------------------------------------------------------- Watching the helper


def test_a_docker_that_cannot_be_asked_does_not_end_an_update_that_runs(updater, su_store, engine):
    handed_over(su_store, UpdatePhase.VERIFYING)
    engine.inspect_error = "Cannot connect to the Docker daemon"

    assert work(updater) is True

    assert su_store.load("u1").phase is UpdatePhase.VERIFYING


def test_a_helper_that_runs_for_too_long_is_removed_and_the_request_ends(updater, su_store, engine, wall):
    handed_over(su_store, UpdatePhase.VERIFYING)
    engine.exists = engine.running = True
    wall.advance(HELPER_MAX_S - 1)
    assert work(updater) is True

    wall.advance(2)
    assert work(updater) is False

    record = su_store.load("u1")
    assert record.outcome is Outcome.FAILED and record.failure is Failure.TIMED_OUT
    assert "was removed" in record.message and engine.exists is False


@pytest.mark.parametrize(
    ("phase", "image", "outcome", "failure", "text"),
    [
        (UpdatePhase.VERIFYING, NEW, Outcome.SUCCEEDED, None, "now running"),
        (UpdatePhase.SWAPPING, NEW, Outcome.SUCCEEDED, None, "now running"),
        (UpdatePhase.ROLLING_BACK, NEW, Outcome.FAILED, Failure.ROLLBACK_FAILED, "The rollback did not finish"),
        (UpdatePhase.ROLLING_BACK, OLD, Outcome.ROLLED_BACK, Failure.VERIFY_FAILED, "runs again"),
        (UpdatePhase.HANDED_OVER, OLD, Outcome.FAILED, Failure.HELPER_FAILED, "still runs"),
        (UpdatePhase.VERIFYING, "sha256:other", Outcome.FAILED, Failure.INTERNAL_ERROR, "neither the old nor the new"),
    ],
)
def test_a_helper_that_ended_without_a_result_is_judged_by_the_manager_that_docker_shows(
    updater, su_store, engine, own_docker, phase, image, outcome, failure, text
):
    handed_over(su_store, phase)
    own_docker.container_info = replace(own_docker.container_info, image_id=image)
    engine.exists, engine.running, engine.exit_code, engine.log = True, False, 137, "Killed"

    assert work(updater) is False

    record = su_store.load("u1")
    assert record.outcome is outcome and record.failure is failure
    assert text in record.message
    assert "exit code 137: Killed" in record.message
    assert engine.exists is False


def test_a_helper_that_is_gone_is_judged_the_same_way(updater, su_store, own_docker):
    handed_over(su_store, UpdatePhase.HANDED_OVER)

    work(updater)

    record = su_store.load("u1")
    assert record.failure is Failure.HELPER_FAILED and "the helper container is gone" in record.message


def test_a_manager_that_cannot_be_read_after_the_helper_ended_is_an_internal_error(
    updater, su_store, own_docker, monkeypatch
):
    handed_over(su_store, UpdatePhase.VERIFYING)

    def unreadable():
        raise DockerError("docker inspect failed")

    monkeypatch.setattr(own_docker, "container", unreadable)

    work(updater)

    record = su_store.load("u1")
    assert record.failure is Failure.INTERNAL_ERROR and "could not be read afterwards" in record.message


def test_a_result_that_the_helper_wrote_just_before_it_ended_is_not_overwritten(updater, su_store, engine, monkeypatch):
    stale = handed_over(su_store, UpdatePhase.VERIFYING)
    finished = SelfUpdate.from_dict(
        {**stale.to_dict(), "phase": "done", "outcome": "succeeded", "message": "the real result"}
    )
    su_store.save(finished)
    monkeypatch.setattr(su_store, "active", lambda: stale)
    engine.exists, engine.running = True, False

    assert work(updater) is False

    assert su_store.load("u1").message == "the real result"
    assert engine.exists is False


# ---------------------------------------------------------------------- Cleaning up


def test_after_a_swap_that_went_well_the_old_image_and_the_stopped_helper_are_removed_once(
    updater, su_store, engine, own_docker
):
    own_docker.images = {f"{REPO}:latest": NEW, f"{REPO}:previous": OLD}
    own_docker.container_info = replace(own_docker.container_info, image_id=NEW)
    ended(su_store, Outcome.SUCCEEDED)
    engine.exists, engine.running = True, False

    assert work(updater) is False

    assert OLD in own_docker.removed
    assert f"{REPO}:previous" not in own_docker.images and own_docker.images[f"{REPO}:latest"] == NEW
    assert engine.exists is False and su_store.load("u1").cleaned_up
    asked = len(own_docker.calls)
    work(updater)
    assert len(own_docker.calls) == asked


def test_the_clean_up_waits_while_the_helper_still_runs(updater, su_store, engine, own_docker):
    ended(su_store, Outcome.SUCCEEDED)
    engine.exists = engine.running = True

    work(updater)

    assert su_store.load("u1").cleaned_up is False and own_docker.calls == []


def test_after_a_rollback_the_image_that_did_not_run_is_removed(updater, su_store, own_docker):
    own_docker.images = {f"{REPO}:latest": OLD, f"{REPO}:v2": NEW}
    ended(su_store, Outcome.ROLLED_BACK)

    work(updater)

    assert own_docker.removed == [NEW]


def test_after_a_failed_rollback_every_image_stays(updater, su_store, own_docker):
    ended(su_store, Outcome.FAILED)

    work(updater)

    assert su_store.load("u1").cleaned_up
    assert own_docker.removed == [] and not any(call[0] == "remove_image" for call in own_docker.calls)


def test_old_records_are_pruned_with_their_compose_files(su_store, store):
    for number in range(12):
        ended(su_store, Outcome.SUCCEEDED, update_id=f"u{number:02d}")
        folder = store.compose_dir / f"self-u{number:02d}"
        folder.mkdir(parents=True)
        (folder / "worker.yml").write_text("services: {}\n", encoding="utf-8")

    su_store.prune(keep=10)

    assert [record.id for record in su_store.all()] == [f"u{number:02d}" for number in range(2, 12)]
    assert not (store.compose_dir / "self-u00").exists() and (store.compose_dir / "self-u11").exists()


def test_a_record_from_a_newer_manager_with_more_fields_is_still_read(su_store):
    request(su_store)
    path = su_store.directory / "u1.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"tag"', '"a_field_of_the_future": 1, "tag"'), encoding="utf-8"
    )

    assert su_store.load("u1").tag == "v2"


def test_two_requests_in_a_row_swap_once_to_the_tag_of_the_last_one(updater, su_store, own_docker, engine):
    own_docker.registry[f"{REPO}:v3"] = NEW
    su_store.submit(SelfUpdate(id="u1", tag="v2", requested_at="2026-01-01T00:00:01.000000+00:00"))
    su_store.submit(SelfUpdate(id="u2", tag="v3", requested_at="2026-01-01T00:00:02.000000+00:00"))

    assert work(updater) is False
    older = su_store.load("u1")
    assert older.outcome is Outcome.UNCHANGED and "the request u2 for tag v3 is newer" in older.message
    assert engine.calls == [] and not any(call[0] == "pull" for call in own_docker.calls)

    assert work(updater) is True
    assert su_store.load("u2").phase is UpdatePhase.HANDED_OVER
    assert ("pull", "v3") in own_docker.calls and ("pull", "v2") not in own_docker.calls


def test_every_finished_request_is_cleaned_up_not_only_the_last_one(updater, su_store, own_docker):
    own_docker.images = {f"{REPO}:latest": NEW, f"{REPO}:previous": OLD}
    own_docker.container_info = replace(own_docker.container_info, image_id=NEW)
    ended(su_store, Outcome.SUCCEEDED, update_id="u1")
    ended(su_store, Outcome.FAILED, update_id="u2")

    work(updater)

    assert [record.cleaned_up for record in su_store.all()] == [True, True]
    assert OLD in own_docker.removed


def test_a_docker_that_cannot_be_asked_for_the_helper_only_delays_the_clean_up(updater, su_store, engine, own_docker):
    ended(su_store, Outcome.SUCCEEDED)
    engine.inspect_error = "Cannot connect to the Docker daemon"

    assert work(updater) is False

    assert su_store.load("u1").cleaned_up is False and own_docker.calls == []
