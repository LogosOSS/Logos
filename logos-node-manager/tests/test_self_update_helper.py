"""The helper: swap the manager, check the new one, and go back if it does not come up."""

from __future__ import annotations

import pytest
from conftest import NEW, OLD, REPO, UNHEALTHY
from self_update_support import WHEN, Wall, handed_over

from logos_node_manager.docker_ops import ContainerInfo
from logos_node_manager.self_update import Failure, SelfUpdate, UpdatePhase
from logos_node_manager.self_update_helper import Swapper, run_helper
from logos_node_manager.state import Outcome


@pytest.fixture
def wall() -> Wall:
    return Wall()


@pytest.fixture
def swapper(own_settings, own_docker, su_store, clock, wall) -> Swapper:
    # The manager pulled the new image before it handed the swap to the helper.
    own_docker.images[f"{REPO}:v2"] = NEW
    return Swapper(own_settings, own_docker, su_store, clock=clock, sleep=clock.sleep, now=wall)


def ups(docker) -> list[tuple]:
    return [call for call in docker.calls if call[0] == "up"]


def new_container(**fields) -> ContainerInfo:
    """The container that Compose made from the new image."""
    values = {
        "id": "c-new",
        "image_id": NEW,
        "image_ref": f"{REPO}:v2",
        "status": "running",
        "running": True,
        "restarting": False,
        "exit_code": 0,
        "restart_count": 0,
        "health": "healthy",
    }
    return ContainerInfo(**{**values, **fields})


def test_the_helper_swaps_checks_and_ends_as_succeeded(swapper, su_store, own_docker):
    record = handed_over(su_store)

    done = swapper.run(record)

    assert done.finished and done.outcome is Outcome.SUCCEEDED
    assert done.message == f"now running {REPO}:v2"
    # The image is on the host already: no pull, and no forced recreate.
    assert ups(own_docker) == [("up", "v2", False, True)]
    assert own_docker.container_info.image_id == NEW
    # The old image has a name of its own, so that the tag that moved cannot leave it without one.
    assert own_docker.images[f"{REPO}:previous"] == OLD
    assert [event["phase"] for event in done.events] == ["swapping", "verifying", "done"]
    assert su_store.load("u1").outcome is Outcome.SUCCEEDED


def test_an_update_that_fails_before_the_manager_stops_leaves_the_old_manager_running(swapper, su_store, own_docker):
    own_docker.up_errors = ["the compose file is invalid"]
    record = handed_over(su_store)

    done = swapper.run(record)

    assert done.outcome is Outcome.FAILED and done.failure is Failure.SWAP_FAILED
    assert "The previous manager is still running." in done.message
    assert len(ups(own_docker)) == 1 and own_docker.container_info.id == "c-old"


def test_an_update_that_fails_after_the_manager_stopped_restores_the_old_one(swapper, su_store, own_docker):
    own_docker.up_errors = ["the new container could not be created"]
    own_docker.failed_up_removes_container = True
    record = handed_over(su_store)

    done = swapper.run(record)

    assert done.outcome is Outcome.ROLLED_BACK and done.failure is Failure.SWAP_FAILED
    assert own_docker.container_info.image_id == OLD
    assert ups(own_docker)[-1] == ("up", "latest", True, True)


def test_a_new_manager_that_does_not_come_up_is_replaced_by_the_old_one_with_its_own_compose_file(
    swapper, su_store, own_docker, tmp_path
):
    new_file, old_file = tmp_path / "new.yml", tmp_path / "old.yml"
    for path in (new_file, old_file):
        path.write_text("services: {}\n", encoding="utf-8")
    own_docker.new_container_timeline = list(UNHEALTHY)
    record = handed_over(su_store, compose_file=str(new_file), rollback_compose_file=str(old_file))

    done = swapper.run(record)

    assert done.outcome is Outcome.ROLLED_BACK and done.failure is Failure.VERIFY_FAILED
    assert "unhealthy" in done.message and "Rolled back to" in done.message
    assert own_docker.container_info.image_id == OLD
    # The way forward uses the file of the new version, and the way back the file that made the old manager.
    files = [call[1] for call in own_docker.calls if call[0] == "use_compose"]
    assert files[0] == new_file and files[-1] == old_file
    # The old image goes back under the name that the service uses, and Compose recreates without a pull.
    assert ("tag", OLD, f"{REPO}:latest") in own_docker.calls
    assert ups(own_docker)[-1] == ("up", "latest", True, True)


def test_a_rollback_that_fails_too_ends_as_failed_and_every_image_stays(swapper, su_store, own_docker):
    own_docker.new_container_timeline = list(UNHEALTHY)
    # The swap works. Every attempt to start the old manager again fails.
    own_docker.up_errors = [None, "boom", "boom", "boom"]
    record = handed_over(su_store)

    done = swapper.run(record)

    assert done.outcome is Outcome.FAILED and done.failure is Failure.ROLLBACK_FAILED
    assert "The rollback failed too" in done.message and "Both images stay on the host." in done.message
    assert OLD in own_docker.host_images() and NEW in own_docker.host_images()


def test_a_compose_that_leaves_the_container_alone_is_reported(swapper, su_store, own_docker):
    # The service names the image that runs already, so Compose has nothing to recreate.
    own_docker.images[f"{REPO}:v2"] = OLD
    record = handed_over(su_store, target_image_id=OLD)

    done = swapper.run(record)

    assert done.outcome is Outcome.FAILED and done.failure is Failure.SWAP_FAILED
    assert "did not replace the container of the manager" in done.message


def test_a_swap_that_was_cut_short_goes_on_with_the_check_and_does_not_replace_the_container_again(
    swapper, su_store, own_docker
):
    own_docker.container_info = new_container()
    record = handed_over(su_store, UpdatePhase.SWAPPING)

    done = swapper.run(record)

    assert done.outcome is Outcome.SUCCEEDED and ups(own_docker) == []
    assert any(event["note"] == "the container was already replaced" for event in done.events)


def test_a_rollback_that_was_cut_short_goes_on_from_the_recorded_phase(swapper, su_store, own_docker):
    own_docker.container_info = new_container(health="unhealthy")
    record = handed_over(su_store, UpdatePhase.ROLLING_BACK, message="the check failed", failure=Failure.VERIFY_FAILED)

    done = swapper.run(record)

    assert done.outcome is Outcome.ROLLED_BACK and own_docker.container_info.image_id == OLD
    assert done.message.startswith("the check failed. Rolled back to")


def test_a_request_that_was_never_handed_over_is_not_swapped(swapper, su_store, own_docker):
    record = SelfUpdate(id="u1", tag="v2", requested_at=WHEN)
    su_store.save(record)

    done = swapper.run(record)

    assert done.outcome is Outcome.FAILED and done.failure is Failure.HELPER_FAILED
    assert ups(own_docker) == []


def test_a_helper_that_keeps_failing_restores_the_previous_manager(swapper, su_store, own_docker, tmp_path):
    record = handed_over(su_store, compose_file=str(tmp_path / "gone.yml"))

    done = swapper.run(record)

    assert done.outcome is Outcome.ROLLED_BACK and done.failure is Failure.INTERNAL_ERROR
    assert "the helper failed 3 times" in done.message and "is missing" in done.message
    assert own_docker.container_info.image_id == OLD


def test_the_helper_command_runs_the_record_and_answers_nothing_for_an_unknown_one(
    own_settings, own_docker, su_store, clock, wall
):
    own_docker.images[f"{REPO}:v2"] = NEW
    handed_over(su_store)

    assert run_helper(own_settings, "nothing", docker=own_docker, store=su_store) is None
    done = run_helper(own_settings, "u1", docker=own_docker, store=su_store, clock=clock, sleep=clock.sleep, now=wall)

    assert done.outcome is Outcome.SUCCEEDED
