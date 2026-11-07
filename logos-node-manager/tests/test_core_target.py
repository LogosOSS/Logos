"""The core target: pull, replace the services, check them, and end as failed without a rollback."""

from __future__ import annotations

from dataclasses import replace
from itertools import count

import pytest
from conftest import queued

from logos_node_manager.config import Settings
from logos_node_manager.core_target import NO_ROLLBACK, CoreTarget
from logos_node_manager.deployer import Deployer
from logos_node_manager.docker_ops import ContainerInfo, ProjectContainer
from logos_node_manager.errors import Interrupted
from logos_node_manager.runner import CommandResult
from logos_node_manager.state import FailureKind, Outcome, Phase, StateStore

SERVICES = ["logos-orchestrator", "logos-webservice", "logos-db"]


def container(cid: str, *, health: str | None = None, **fields) -> ContainerInfo:
    values = {
        "id": cid,
        "image_id": f"sha256:{cid}",
        "image_ref": "registry/logos/x:latest",
        "status": "running",
        "running": True,
        "restarting": False,
        "exit_code": 0,
        "restart_count": 0,
        "health": health,
    }
    values.update(fields)
    return ContainerInfo(**values)


class FakeProject:
    """The compose project as the core target sees it. Every field is a knob a test can turn."""

    def __init__(self) -> None:
        self.ids = count(1)
        self.containers = [
            ProjectContainer("logos-orchestrator", container("old-orch")),
            ProjectContainer("logos-webservice", container("old-web")),
            ProjectContainer("logos-db", container("old-db", health="healthy", restart_count=3)),
        ]
        self.service_names = list(SERVICES)
        self.replica_count = 1
        self.pull_error: str | None = None
        self.up_error: str | None = None
        # What ``up`` does to the containers. None: replace the two Logos services. A function can do anything else.
        self.on_up = self.replace_logos_services
        # Containers handed out one per call of project_containers, then the last one stays.
        self.timeline: list[list[ProjectContainer]] = []
        self.calls: list[tuple] = []
        self.logs = "service log line"
        self.prune_error = False
        self.pull_image_error = False

    def replace_logos_services(self) -> None:
        self.containers = [
            ProjectContainer("logos-orchestrator", container(f"new-orch-{next(self.ids)}")),
            ProjectContainer("logos-webservice", container(f"new-web-{next(self.ids)}")),
            self.containers[2],
        ]

    # ------------------------------------------------------------------ the ProjectDocker interface

    def pull_project(self, tag: str) -> CommandResult:
        self.calls.append(("pull_project", tag))
        if self.pull_error:
            return CommandResult(("pull",), 1, stderr=self.pull_error)
        return CommandResult(("pull",), 0)

    def up_project(self, tag: str, replicas: int) -> CommandResult:
        self.calls.append(("up_project", tag, replicas))
        if self.up_error:
            return CommandResult(("up",), 1, stderr=self.up_error)
        self.on_up()
        return CommandResult(("up",), 0)

    def replicas(self) -> int:
        return self.replica_count

    def services(self) -> list[str]:
        return list(self.service_names)

    def project_containers(self) -> list[ProjectContainer]:
        if self.timeline:
            current = self.timeline[0]
            if len(self.timeline) > 1:
                self.timeline.pop(0)
            return current
        return list(self.containers)

    def service_logs(self, services: list[str], lines: int = 30) -> str:
        self.calls.append(("service_logs", tuple(services)))
        return self.logs

    def prune_images(self) -> CommandResult:
        self.calls.append(("prune_images",))
        return CommandResult(("prune",), 1 if self.prune_error else 0, stderr="denied" if self.prune_error else "")

    def pull_image(self, reference: str) -> CommandResult:
        self.calls.append(("pull_image", reference))
        return CommandResult(("pull",), 1 if self.pull_image_error else 0, stderr="denied")


@pytest.fixture
def core_settings(tmp_path) -> Settings:
    (tmp_path / "project").mkdir()
    return Settings.from_env(
        {
            "NODE_MANAGER_KIND": "core",
            "NODE_MANAGER_PROJECT_DIR": str(tmp_path / "project"),
            "NODE_MANAGER_STATE_DIR": str(tmp_path / "state"),
            "NODE_MANAGER_STABILITY_WINDOW_S": "10",
            "NODE_MANAGER_POLL_INTERVAL_S": "5",
            "NODE_MANAGER_CORE_VERIFY_TIMEOUT_S": "60",
        }
    )


@pytest.fixture
def project() -> FakeProject:
    return FakeProject()


@pytest.fixture
def probe_answers() -> dict:
    return {"ok": True, "urls": []}


@pytest.fixture
def make_core(core_settings, project, clock, probe_answers):
    def build(settings: Settings | None = None, **overrides) -> tuple[Deployer, StateStore]:
        settings = settings or core_settings
        store = StateStore(settings.state_dir)

        def probe(url: str) -> bool:
            probe_answers["urls"].append(url)
            return probe_answers["ok"]

        deployer = Deployer(
            settings,
            None,
            store,
            target_factory=lambda d: CoreTarget(d, project, probe=probe),
            clock=clock,
            sleep=clock.sleep,
            now=lambda: "2026-01-01T00:00:00.000000+00:00",
            **overrides,
        )
        return deployer, store

    return build


def run(make_core, tag: str = "v2", settings: Settings | None = None):
    deployer, store = make_core(settings)
    record = deployer.process(queued(store, tag))
    return record


def test_a_good_deployment_replaces_checks_and_cleans_up(make_core, project):
    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED
    assert record.phase is Phase.DONE
    assert record.message == "now running tag v2"
    assert [call[0] for call in project.calls] == ["pull_project", "up_project", "prune_images"]
    assert project.calls[1] == ("up_project", "v2", 1)


def test_the_record_notes_the_containers_that_ran_before(make_core):
    record = run(make_core)

    assert record.previous_container_ids == ["old-orch", "old-web", "old-db"]
    assert record.events[0]["note"] == "3 of 3 container(s) run now; deploying tag v2"


def test_the_replica_count_is_passed_to_the_scaled_service(make_core, project):
    project.replica_count = 2
    project.on_up = lambda: setattr(
        project,
        "containers",
        [
            ProjectContainer("logos-orchestrator", container("new-orch")),
            ProjectContainer("logos-webservice", container("new-web-1")),
            ProjectContainer("logos-webservice", container("new-web-2")),
            ProjectContainer("logos-db", container("old-db", health="healthy")),
        ],
    )

    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED
    assert ("up_project", "v2", 2) in project.calls


def test_a_failed_pull_leaves_the_services_alone(make_core, project):
    project.pull_error = "denied: unauthorized"

    record = run(make_core)

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.PULL_FAILED
    assert "denied: unauthorized" in record.message
    assert "were not touched" in record.message
    assert [call[0] for call in project.calls] == ["pull_project"]


def test_a_failed_up_ends_as_failed_without_a_rollback(make_core, project):
    project.up_error = "port is already allocated"

    record = run(make_core)

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.SWAP_FAILED
    assert "port is already allocated" in record.message
    assert NO_ROLLBACK in record.message
    assert ("prune_images",) not in project.calls


def test_nothing_to_replace_ends_as_unchanged(make_core, project):
    project.on_up = lambda: None

    record = run(make_core)

    assert record.outcome is Outcome.UNCHANGED
    assert record.message == "every service already runs this version"
    assert ("prune_images",) not in project.calls


def test_a_new_container_that_restarted_fails_the_deployment_with_its_log(make_core, project):
    def crash_loop() -> None:
        project.replace_logos_services()
        web = project.containers[1]
        project.containers[1] = ProjectContainer(web.service, container(web.info.id, restart_count=2))

    project.on_up = crash_loop

    record = run(make_core)

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.VERIFY_FAILED
    assert record.message.startswith("logos-webservice restarted 2 time(s)")
    assert NO_ROLLBACK in record.message
    assert record.failure_logs == "service log line"
    assert ("service_logs", ("logos-webservice",)) in project.calls
    assert ("prune_images",) not in project.calls


def test_an_old_container_with_old_restarts_is_no_problem(make_core, project):
    # The database container of the fixture ran before, with restart_count 3, and keeps running.
    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED


def test_an_unhealthy_container_fails_the_deployment(make_core, project):
    def unhealthy() -> None:
        project.replace_logos_services()
        project.containers[2] = ProjectContainer("logos-db", container("old-db", health="unhealthy"))

    project.on_up = unhealthy

    record = run(make_core)

    assert record.failure is FailureKind.VERIFY_FAILED
    assert record.message.startswith("the health check reports logos-db as unhealthy")


def test_a_service_that_comes_up_late_is_waited_for(make_core, project, clock):
    project.replace_logos_services()
    complete = list(project.containers)
    without_web = [complete[0], complete[2]]
    project.on_up = lambda: None
    project.timeline = [without_web, without_web, complete]
    # ``begin`` and the check after ``up`` read the ids of the old containers: give them the old ones first.
    old = [
        ProjectContainer("logos-orchestrator", container("old-orch")),
        ProjectContainer("logos-webservice", container("old-web")),
        ProjectContainer("logos-db", complete[2].info),
    ]
    project.timeline = [old, without_web, without_web, without_web, complete]

    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED
    assert clock.now > 0


def test_services_that_never_come_up_fail_after_the_time_limit(make_core, project):
    project.on_up = lambda: setattr(project, "containers", project.containers[:1] + project.containers[2:])

    record = run(make_core)

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.VERIFY_FAILED
    assert "not ready after 60 s" in record.message
    assert "logos-webservice runs 0 of 1 ready copy" in record.message


def test_the_address_of_logos_must_answer(core_settings, make_core, probe_answers):
    settings = replace(core_settings, core_health_url="https://logos.example/api/info")
    probe_answers["ok"] = False

    record = run(make_core, settings=settings)

    assert record.outcome is Outcome.FAILED
    assert "https://logos.example/api/info does not answer yet" in record.message
    assert set(probe_answers["urls"]) == {"https://logos.example/api/info"}


def test_the_address_is_not_asked_when_none_is_set(make_core, probe_answers):
    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED
    assert probe_answers["urls"] == []


def test_a_step_repeated_after_the_replacement_is_not_taken_for_unchanged(make_core, project):
    deployer, store = make_core()
    record = queued(store)
    # The manager stopped after ``up`` replaced the containers but before the record moved on.
    deployer.target.begin(record)
    project.replace_logos_services()
    record.phase = Phase.SWAPPING
    store.save(record)

    deployer.target.swap(record)

    assert record.phase is Phase.VERIFYING


def test_the_images_stay_after_a_failure(make_core, project):
    project.up_error = "boom"

    run(make_core)

    assert ("prune_images",) not in project.calls
    assert not [call for call in project.calls if call[0] == "pull_image"]


def test_the_agent_image_is_pulled_after_the_prune(core_settings, make_core, project):
    core_settings.env_file.write_text('COMPOSE_PROFILES="agent"\nREGISTRY="registry.example/logos"\n', encoding="utf-8")

    run(make_core)

    assert project.calls[-2:] == [
        ("prune_images",),
        ("pull_image", "registry.example/logos/logos-agent-workspace:v2"),
    ]


def test_no_agent_image_is_pulled_without_the_agent_profile(core_settings, make_core, project):
    core_settings.env_file.write_text("COMPOSE_PROFILES=monitoring\n", encoding="utf-8")

    run(make_core)

    assert not [call for call in project.calls if call[0] == "pull_image"]


def test_a_failing_clean_up_never_changes_the_result(make_core, project):
    project.prune_error = True

    record = run(make_core)

    assert record.outcome is Outcome.SUCCEEDED


def test_the_deployment_stops_between_steps_when_asked(make_core):
    deployer, store = make_core(should_stop=lambda: True)

    with pytest.raises(Interrupted):
        deployer.process(queued(store))


def test_a_record_in_the_rollback_phase_ends_as_failed(make_core):
    deployer, store = make_core()
    record = queued(store)
    record.phase = Phase.ROLLING_BACK
    record.message = "the check failed"

    deployer.target.roll_back(record)

    assert record.outcome is Outcome.FAILED
    assert record.failure is FailureKind.ROLLBACK_FAILED
    assert NO_ROLLBACK in record.message
