"""The notification that a node manager's report of a deployment turns into.

Only the ending is reported, and it goes to the Logos admins. A success is the ``deployment`` type, everything that a
person has to look at is ``deployment_failed``. The inbox shows two lines of the body, so the body stays short.
"""

from __future__ import annotations

import pytest

from logos.dbutils.dbrequest import LogosNodeDeploymentReport
from logos.notifications.deployment import deployment_notification

CI_RUN = "https://github.com/org/repo/actions/runs/123456"


def _report(**fields) -> LogosNodeDeploymentReport:
    fields.setdefault("tag", "v2")
    fields.setdefault("outcome", "succeeded")
    return LogosNodeDeploymentReport(shared_key="key", deployment_id="d1", **fields)


def test_a_success_says_how_many_requests_the_drain_waited_for():
    notification = deployment_notification(
        "node-a",
        _report(message="now running r/w:v2", drained_requests=3, drain_seconds=42, requested_by=CI_RUN),
    )

    assert notification["notification_type"] == "deployment"
    assert notification["title"] == "node-a now runs v2"
    assert notification["body"] == "3 requests were running; drained in 42 s.\nrun 123456"
    assert notification["targets"] == [{"kind": "role", "role": "logos_admin"}]
    assert notification["payload"]["drained_requests"] == 3
    assert notification["payload"]["drain_seconds"] == 42


def test_one_request_is_not_called_requests():
    notification = deployment_notification("node-a", _report(drained_requests=1, drain_seconds=12))

    assert notification["body"] == "1 request was running; drained in 12 s."


def test_a_deployment_that_replaced_nothing_is_a_success_that_says_so():
    notification = deployment_notification(
        "node-a", _report(outcome="unchanged", message="the container already runs r/w:v2")
    )

    assert notification["notification_type"] == "deployment"
    assert notification["title"] == "node-a already runs v2"


def test_the_body_does_not_repeat_the_image_of_the_title():
    notification = deployment_notification("node-a", _report(message="now running registry.example/r/w:v2"))

    assert notification["body"] is None


def test_an_idle_node_is_said_to_have_had_nothing_to_wait_for():
    notification = deployment_notification("node-a", _report(drained_requests=0, drain_seconds=10))

    assert notification["body"] == "No requests were running; drained in 10 s."


def test_a_deployment_without_a_drain_says_nothing_about_requests():
    notification = deployment_notification("node-a", _report(message="now running r/w:v2", requested_by=CI_RUN))

    assert "request" not in notification["body"]


def test_the_models_that_waited_for_the_node_are_counted_and_listed_in_the_payload():
    notification = deployment_notification("node-a", _report(sole_replica_models=["m1", "m2"], requested_by=CI_RUN))

    assert notification["body"] == "2 models had no other node · run 123456"
    assert notification["payload"]["sole_replica_models"] == ["m1", "m2"]


def test_one_model_is_not_called_models():
    notification = deployment_notification("node-a", _report(sole_replica_models=["m1"]))

    assert notification["body"] == "1 model had no other node"


def test_a_requester_that_is_not_a_ci_run_is_kept_but_cut_to_a_line():
    notification = deployment_notification("node-a", _report(requested_by="x" * 200))

    assert notification["body"] == "x" * 60
    assert notification["payload"]["requested_by"] == "x" * 200


@pytest.mark.parametrize(
    ("outcome", "failure", "title"),
    [
        ("rolled_back", "verify_failed", "node-a was rolled back after deploying v2"),
        ("failed", "drain_timeout", "Deploying v2 on node-a failed"),
        ("failed", "rollback_failed", "node-a needs manual action: deploying v2 and its rollback failed"),
    ],
)
def test_what_a_person_has_to_look_at_is_a_failed_deployment(outcome, failure, title):
    notification = deployment_notification("node-a", _report(outcome=outcome, failure=failure, message="why"))

    assert notification["notification_type"] == "deployment_failed"
    assert notification["title"] == title
    assert notification["body"] == "why"


def test_a_failure_names_its_reason_and_the_run_and_keeps_the_whole_message_in_the_payload():
    message = "the node did not drain within 900 s: 2 request(s) still running. The cordon was lifted."
    notification = deployment_notification(
        "node-a", _report(outcome="failed", failure="drain_timeout", message=message, requested_by=CI_RUN)
    )

    assert notification["body"] == "the node did not drain within 900 s: 2 request(s) still running.\nrun 123456"
    assert notification["payload"]["message"] == message


def test_a_failure_with_a_long_output_is_cut_to_a_line():
    message = "docker compose pull failed: " + "x" * 500

    notification = deployment_notification("node-a", _report(outcome="failed", failure="pull_failed", message=message))

    assert len(notification["body"]) == 100
    assert notification["body"].endswith("…")


def test_a_title_never_exceeds_the_column_of_the_application_server():
    notification = deployment_notification("n" * 400, _report(outcome="failed", failure="pull_failed"))

    assert len(notification["title"]) == 255
