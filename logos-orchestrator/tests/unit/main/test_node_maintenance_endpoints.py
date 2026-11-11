"""The endpoints a node manager uses to cordon its node while it replaces the worker container.

The manager authenticates with the key of the worker, so the orchestrator resolves the node from the key and a caller
cannot act on another node. Like the other worker endpoints, they require TLS because the key travels in the body.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

# Reuse the registry double of the service tests
from tests.unit.test_node_maintenance import FakeClock, FakeRegistry

import logos as main_mod
from logos.dbutils.dbrequest import LogosNodeDeploymentReport as DeploymentReport
from logos.dbutils.dbrequest import LogosNodeMaintenanceRequest
from logos.node_maintenance import NodeMaintenance
from logos.routers import logosnode as logosnode_mod

KEYS = {"key-a": 1, "key-b": 2}


def _request(scheme: str = "https") -> MagicMock:
    request = MagicMock()
    request.url.scheme = scheme
    request.headers.get = lambda key, default="": default
    return request


def _body(key: str = "key-a", deployment_id: str = "d1") -> LogosNodeMaintenanceRequest:
    return LogosNodeMaintenanceRequest(shared_key=key, deployment_id=deployment_id)


@pytest.fixture
def service(monkeypatch) -> NodeMaintenance:
    registry = FakeRegistry()
    registry.add(1, "node-a", ["model-a"])
    registry.add(2, "node-b", ["model-b"])
    service = NodeMaintenance(registry, ttl_s=600, settle_s=10, clock=FakeClock())
    monkeypatch.setattr(main_mod, "_node_maintenance", service)

    def provider_for_key(shared_key: str) -> dict:
        if shared_key not in KEYS:
            raise HTTPException(status_code=404, detail="Provider not found for this API key")
        return {"id": KEYS[shared_key]}

    monkeypatch.setattr(logosnode_mod, "_logosnode_provider_for_key", provider_for_key)
    return service


@pytest.mark.asyncio
async def test_cordon_resolves_the_node_from_the_key(service):
    answer = await logosnode_mod.logosnode_maintenance_cordon(_body("key-b"), _request())

    assert answer["status"] == "granted"
    assert service.is_cordoned(2)
    assert not service.is_cordoned(1)


@pytest.mark.asyncio
async def test_a_key_that_belongs_to_no_node_is_rejected(service):
    with pytest.raises(HTTPException) as exc_info:
        await logosnode_mod.logosnode_maintenance_cordon(_body("unknown"), _request())

    assert exc_info.value.status_code == 404
    assert not service.is_cordoned(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["cordon", "status", "release"])
async def test_the_key_must_not_travel_over_plain_http(service, call):
    endpoint = getattr(logosnode_mod, f"logosnode_maintenance_{call}")

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(_body(), _request(scheme="http"))

    assert exc_info.value.status_code == 400
    assert not service.is_cordoned(1)


@pytest.mark.asyncio
async def test_a_node_cannot_release_the_lease_of_another_node(service):
    await logosnode_mod.logosnode_maintenance_cordon(_body("key-a", "d1"), _request())

    answer = await logosnode_mod.logosnode_maintenance_release(_body("key-b", "d1"), _request())

    assert answer == {"status": "no_lease"}
    assert service.is_cordoned(1)


@pytest.mark.asyncio
async def test_the_second_node_is_denied_until_the_first_released(service):
    await logosnode_mod.logosnode_maintenance_cordon(_body("key-a", "d1"), _request())

    denied = await logosnode_mod.logosnode_maintenance_cordon(_body("key-b", "d2"), _request())
    await logosnode_mod.logosnode_maintenance_release(_body("key-a", "d1"), _request())
    granted = await logosnode_mod.logosnode_maintenance_cordon(_body("key-b", "d2"), _request())

    assert denied["status"] == "denied"
    assert denied["holder"] == "node-a"
    assert granted["status"] == "granted"


@pytest.mark.asyncio
async def test_status_reports_the_session_the_manager_waits_for(service):
    await logosnode_mod.logosnode_maintenance_cordon(_body(), _request())

    answer = await logosnode_mod.logosnode_maintenance_status(_body(), _request())

    assert answer["status"] == "leased"
    assert answer["online"] is True
    assert answer["session_id"] == "session-1"
    assert answer["first_status_received"] is True


@pytest.mark.asyncio
async def test_release_ends_the_cordon(service):
    await logosnode_mod.logosnode_maintenance_cordon(_body(), _request())

    answer = await logosnode_mod.logosnode_maintenance_release(_body(), _request())

    assert answer == {"status": "released"}
    assert not service.is_cordoned(1)


@pytest.mark.parametrize("deployment_id", ["123456789-2", "a", "run.7_attempt-2", "A" * 64])
def test_a_deployment_id_of_the_node_manager_is_accepted(deployment_id):
    assert _body(deployment_id=deployment_id).deployment_id == deployment_id


@pytest.mark.parametrize(
    "deployment_id", ["", "-starts-with-dash", "has space", "semi;colon", "x" * 65, "new\nline", "trailing\n"]
)
def test_anything_else_is_not_a_deployment_id(deployment_id):
    with pytest.raises(ValidationError):
        _body(deployment_id=deployment_id)


# ── the report of how a deployment ended ─────────────────────────────────────


def _report(key: str = "key-a", deployment_id: str = "d1", **fields) -> DeploymentReport:
    fields.setdefault("tag", "v2")
    fields.setdefault("outcome", "succeeded")
    return DeploymentReport(shared_key=key, deployment_id=deployment_id, **fields)


class _Emitter(list):
    """Stands in for ``emit_notification``: records the calls, and answers whether it delivered."""

    delivered = True

    async def __call__(self, **kwargs) -> bool:
        self.append(kwargs)
        return self.delivered


@pytest.fixture
def emitted(monkeypatch) -> _Emitter:
    emitter = _Emitter()
    monkeypatch.setattr(logosnode_mod, "emit_notification", emitter)
    return emitter


@pytest.mark.asyncio
async def test_a_report_becomes_one_notification_for_the_logos_admins(service, emitted):
    service.request(1, "d1")
    answer = await logosnode_mod.logosnode_maintenance_report(_report(), _request())

    assert answer == {"status": "accepted"}
    assert len(emitted) == 1
    assert emitted[0]["notification_type"] == "deployment"
    assert emitted[0]["targets"] == [{"kind": "role", "role": "logos_admin"}]
    # The node is named by the orchestrator from the key, not by the caller.
    assert emitted[0]["payload"]["node"] == "node-a"


@pytest.mark.asyncio
async def test_a_repeated_report_carries_the_same_key_so_the_application_server_stores_it_once(service, emitted):
    service.request(1, "d1")
    await logosnode_mod.logosnode_maintenance_report(_report(), _request())

    answer = await logosnode_mod.logosnode_maintenance_report(_report(), _request())

    # The application server stores one notification per key, whatever the orchestrator remembers.
    assert answer == {"status": "accepted"}
    assert [call["event_key"] for call in emitted] == ["deployment:1:d1", "deployment:1:d1"]


@pytest.mark.asyncio
async def test_the_same_deployment_id_on_two_nodes_is_two_notifications(service, emitted):
    service.request(1, "d1")
    service.release(1, "d1")
    service.request(2, "d1")
    service.release(2, "d1")
    await logosnode_mod.logosnode_maintenance_report(_report("key-a"), _request())
    await logosnode_mod.logosnode_maintenance_report(_report("key-b"), _request())

    assert [call["payload"]["node"] for call in emitted] == ["node-a", "node-b"]
    assert [call["event_key"] for call in emitted] == ["deployment:1:d1", "deployment:2:d1"]


@pytest.mark.asyncio
async def test_a_notification_that_was_not_delivered_can_be_reported_again(service, emitted):
    service.request(1, "d1")
    emitted.delivered = False
    with pytest.raises(HTTPException) as exc_info:
        await logosnode_mod.logosnode_maintenance_report(_report(), _request())
    emitted.delivered = True

    answer = await logosnode_mod.logosnode_maintenance_report(_report(), _request())

    assert exc_info.value.status_code == 503
    assert answer == {"status": "accepted"}
    assert emitted[0]["event_key"] == emitted[1]["event_key"]


@pytest.mark.asyncio
async def test_a_repeat_during_a_slow_delivery_is_delivered_again_when_the_first_one_fails(service, monkeypatch):
    """The manager gives up on a slow call and repeats it. The repeat must not be taken for a finished report."""
    service.request(1, "d1")
    release_first = asyncio.Event()
    calls: list[dict] = []

    async def emit(**kwargs) -> bool:
        calls.append(kwargs)
        if len(calls) == 1:
            await release_first.wait()
            return False
        return True

    monkeypatch.setattr(logosnode_mod, "emit_notification", emit)
    first = asyncio.create_task(logosnode_mod.logosnode_maintenance_report(_report(), _request()))
    while not calls:
        await asyncio.sleep(0.01)

    answer = await logosnode_mod.logosnode_maintenance_report(_report(), _request())
    release_first.set()
    with pytest.raises(HTTPException):
        await first

    assert answer == {"status": "accepted"}
    assert len(calls) == 2
    assert calls[0]["event_key"] == calls[1]["event_key"]


@pytest.mark.asyncio
async def test_the_report_must_not_travel_over_plain_http(service, emitted):
    with pytest.raises(HTTPException) as exc_info:
        await logosnode_mod.logosnode_maintenance_report(_report(), _request(scheme="http"))

    assert exc_info.value.status_code == 400
    assert emitted == []


@pytest.mark.asyncio
async def test_a_key_that_belongs_to_no_node_reports_nothing(service, emitted):
    with pytest.raises(HTTPException) as exc_info:
        await logosnode_mod.logosnode_maintenance_report(_report("unknown"), _request())

    assert exc_info.value.status_code == 404
    assert emitted == []


@pytest.mark.asyncio
async def test_a_deployment_that_changed_nothing_is_reported_as_a_success(service, emitted):
    service.request(1, "d1")
    answer = await logosnode_mod.logosnode_maintenance_report(_report(outcome="unchanged"), _request())

    assert answer == {"status": "accepted"}
    assert emitted[0]["notification_type"] == "deployment"


def test_an_outcome_that_is_not_an_ending_is_not_accepted():
    with pytest.raises(ValidationError):
        _report(outcome="running")


@pytest.mark.parametrize("tag", ["v1.2-rc_1", "_build", "A" * 128])
def test_reports_accept_node_manager_tag_grammar(tag):
    assert _report(tag=tag).tag == tag


@pytest.mark.parametrize("tag", ["", "-tag", ".tag", "has space", "tag\n", "tag/extra", "x" * 129, "<b>tag</b>"])
def test_reports_reject_invalid_tags(tag):
    with pytest.raises(ValidationError):
        _report(tag=tag)


@pytest.mark.parametrize("field, limit", [("message", 2000), ("requested_by", 300)])
def test_report_text_is_bounded(field, limit):
    assert getattr(_report(**{field: "x" * limit}), field) == "x" * limit
    with pytest.raises(ValidationError):
        _report(**{field: "x" * (limit + 1)})


async def test_reports_require_a_granted_lease_on_the_authenticated_node(service, emitted):
    service.request(1, "d1")
    assert service.request(2, "d1")["status"] == "denied"
    for body in [_report("key-b"), _report(deployment_id="never-granted")]:
        assert await logosnode_mod.logosnode_maintenance_report(body, _request()) == {"status": "dropped"}
    assert emitted == []


@pytest.mark.parametrize("end", ["release", "expire", "admin"])
async def test_lease_evidence_survives_the_end_of_the_lease(service, emitted, end):
    service.request(1, "d1")
    if end == "expire":
        service._clock.advance(7200)
        assert not service.is_cordoned(1)
    elif end == "admin":
        service.admin_release(1, "d1")
    else:
        service.release(1, "d1")
    assert await logosnode_mod.logosnode_maintenance_report(_report(), _request()) == {"status": "accepted"}
    assert len(emitted) == 1


async def test_report_limit_covers_new_ids_retries_and_concurrent_calls_per_node(service, emitted, caplog):
    for i in range(21):
        service.request(1, f"d{i}")
        service.release(1, f"d{i}")
    answers = await asyncio.gather(
        *[logosnode_mod.logosnode_maintenance_report(_report(deployment_id=f"d{i}"), _request()) for i in range(21)]
    )
    assert [a["status"] for a in answers].count("accepted") == 20
    assert [a["status"] for a in answers].count("dropped") == 1
    assert len(emitted) == 20
    assert "hourly report limit reached" in caplog.text
    assert await logosnode_mod.logosnode_maintenance_report(_report(), _request()) == {"status": "dropped"}
    service.request(2, "d1")
    assert await logosnode_mod.logosnode_maintenance_report(_report("key-b"), _request()) == {"status": "accepted"}
    service._clock.advance(3599)
    assert await logosnode_mod.logosnode_maintenance_report(_report(), _request()) == {"status": "dropped"}
    service._clock.advance(1)
    assert await logosnode_mod.logosnode_maintenance_report(_report(), _request()) == {"status": "accepted"}


@pytest.mark.parametrize("role", [None, "app_developer", "app_admin", "logos_admin"])
async def test_operator_endpoints_require_a_logos_admin_key(service, monkeypatch, role):
    from logos.dbutils.dbrequest import AdminReleaseNodeMaintenanceRequest, LogosKeyModel

    db = MagicMock()
    db.get_user_by_api_key.return_value = {"role": role} if role else None
    manager = MagicMock()
    manager.__enter__.return_value = db
    monkeypatch.setattr(logosnode_mod, "DBManager", lambda: manager)
    service.request(1, "d1")
    for endpoint, body in [
        (logosnode_mod.logosnode_maintenance_admin_status, LogosKeyModel(logos_key="operator")),
        (
            logosnode_mod.logosnode_maintenance_admin_release,
            AdminReleaseNodeMaintenanceRequest(logos_key="operator", provider_id=1, deployment_id="d1"),
        ),
    ]:
        if role == "logos_admin":
            await endpoint(body)
        else:
            with pytest.raises(HTTPException) as exc:
                await endpoint(body)
            assert exc.value.status_code == 403
    assert service.is_cordoned(1) is (role != "logos_admin")


async def test_failed_delivery_attempts_also_consume_the_report_budget(service, emitted):
    service._reports_per_hour = 1
    service.request(1, "d1")
    emitted.delivered = False
    with pytest.raises(HTTPException) as exc:
        await logosnode_mod.logosnode_maintenance_report(_report(), _request())
    assert exc.value.status_code == 503
    assert await logosnode_mod.logosnode_maintenance_report(_report(), _request()) == {"status": "dropped"}
    assert len(emitted) == 1
