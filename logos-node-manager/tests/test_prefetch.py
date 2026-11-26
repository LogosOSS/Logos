"""The early download: the image of an announced deployment, taken before the deployment is ordered."""

from __future__ import annotations

import logging

import pytest
from conftest import NEW, OLD, REPO, UNHEALTHY, FakeEngine, queued

from logos_node_manager.compose_source import ComposeSource
from logos_node_manager.state import Outcome

HELD = f"{REPO}:prefetched"
NEWER = "sha256:" + "3" * 12 + "dddd"


def test_the_name_of_a_moving_tag_stays_on_the_image_it_had(deployer, docker, store):
    deployer.prefetch("latest")

    # A "docker compose up" by hand starts what it would have started before.
    assert docker.images[f"{REPO}:latest"] == OLD
    assert docker.images[HELD] == NEW
    assert docker.container_info.id == "c-old"
    assert not any(call[0] == "up" for call in docker.calls)
    assert store.all() == []


def test_a_tag_that_was_not_on_the_host_gets_no_name_before_its_deployment(deployer, docker):
    deployer.prefetch("v2")

    assert f"{REPO}:v2" not in docker.images
    assert docker.images[HELD] == NEW


def test_the_deployment_takes_over_the_downloaded_image(deployer, docker, store):
    deployer.prefetch("v2")

    record = deployer.process(queued(store, "v2"))

    assert record.outcome is Outcome.SUCCEEDED
    assert docker.container_info.image_id == NEW
    assert docker.images[f"{REPO}:v2"] == NEW
    assert HELD not in docker.images


def test_a_release_that_was_downloaded_early_is_still_removed_after_a_rollback(deployer, docker, store):
    # An image with a second name would stay on the host: the clean-up removes an image by its ID.
    docker.new_container_timeline = list(UNHEALTHY)
    deployer.prefetch("v2")

    record = deployer.process(queued(store, "v2"))

    assert record.outcome is Outcome.ROLLED_BACK
    assert NEW not in docker.host_images()


def test_the_next_download_replaces_the_one_that_no_deployment_took(deployer, docker):
    docker.registry[f"{REPO}:v3"] = NEWER
    deployer.prefetch("v2")

    deployer.prefetch("v3")

    assert docker.images[HELD] == NEWER
    assert docker.host_images() == {OLD, NEWER}


def test_an_image_that_the_tag_already_names_changes_nothing(deployer, docker):
    docker.registry[f"{REPO}:latest"] = OLD
    names = dict(docker.images)

    deployer.prefetch("latest")

    assert docker.images == names


def test_a_download_that_fails_changes_nothing(deployer, docker, caplog):
    docker.pull_error = "unauthorized: authentication required"
    names = dict(docker.images)

    with caplog.at_level(logging.WARNING):
        deployer.prefetch("v2")

    assert docker.images == names
    assert "unauthorized" in caplog.text


@pytest.mark.parametrize("from_tag", [True, False])
def test_the_download_uses_the_compose_file_that_ran_last(make_deployer, settings, docker, store, from_tag):
    # The compose file of the host can be out of date on a host that takes the file from the image of the tag.
    settings.env_file.parent.mkdir(parents=True, exist_ok=True)
    settings.env_file.write_text(f"NODE_MANAGER_COMPOSE_FROM_TAG={str(from_tag).lower()}\n", encoding="utf-8")
    last = store.compose_dir / ".current" / "worker.yml"
    last.parent.mkdir(parents=True)
    last.write_text("services: {}\n", encoding="utf-8")
    deployer = make_deployer(compose=ComposeSource(settings, FakeEngine(), store.compose_dir))

    deployer.prefetch("v2")

    assert docker.compose_path == (last if from_tag else None)
