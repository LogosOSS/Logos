"""The order of a deployment from the Logos on dev, and the wait for it.

The tests pin where the key goes, which answers end an order, and when a
deployment counts as succeeded — while the core of dev is being replaced, dev
answers with errors that must end neither the order nor the wait.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from app import dev_deployments

DEV = "https://dev.example"


def fake_dev(monkeypatch, *answers):
    """Answer each request with the next of ``answers``; the last one repeats."""
    calls: list = []
    queue = list(answers)

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, headers=None, json=None):
            calls.append({"method": method, "url": url, "headers": headers, "json": json})
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

    monkeypatch.setattr(dev_deployments.httpx, "AsyncClient", FakeClient)
    configure(monkeypatch)
    return calls


def configure(monkeypatch, **changes):
    values = {"deploy_enabled": True, "dev_base_url": f"{DEV}/", "dev_deployment_key": "key-for-dev", **changes}
    monkeypatch.setattr(dev_deployments, "settings", replace(dev_deployments.settings, **values))


def deployment(status="running", core="succeeded", worker="running", reason=None):
    return httpx.Response(
        200,
        json={
            "id": "plan-1",
            "status": status,
            "reason": reason,
            "steps": [{"kind": "core", "status": core}, {"kind": "worker", "status": worker}],
        },
    )


BUSY = httpx.Response(409, json={"detail": "A deployment is running."})


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"deploy_enabled": False}, "LOGOS_AGENT_DEPLOY_ENABLED"),
        ({"dev_deployment_key": ""}, "LOGOS_AGENT_DEV_DEPLOYMENT_KEY"),
    ],
)
def test_a_runner_that_cannot_deploy_says_which_setting_is_missing(monkeypatch, changes, named):
    configure(monkeypatch, **changes)

    assert named in dev_deployments.missing_setting()


def test_a_configured_runner_misses_nothing(monkeypatch):
    configure(monkeypatch)

    assert dev_deployments.missing_setting() is None


async def test_the_order_carries_the_tag_and_the_key_to_the_logos_on_dev(monkeypatch):
    calls = fake_dev(monkeypatch, httpx.Response(201, json={"id": "plan-1", "number": 7}))

    assert await dev_deployments.start("pr-93") == {"id": "plan-1", "number": 7}

    assert calls == [
        {
            "method": "POST",
            "url": f"{DEV}/api/deployments/agent/plans",
            "headers": {"Authorization": "Bearer key-for-dev"},
            "json": {"tag": "pr-93"},
        }
    ]
    assert dev_deployments.page_url("plan-1") == f"{DEV}/deployments/plan-1"


async def test_an_order_waits_while_another_deployment_runs_or_dev_does_not_answer(monkeypatch):
    calls = fake_dev(
        monkeypatch,
        BUSY,
        httpx.ConnectError("connection refused"),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(201, json={"id": "plan-2"}),
    )
    waits: list = []

    async def on_wait(reason):
        waits.append(reason)

    assert await dev_deployments.start("pr-93", on_wait=on_wait, poll_s=0) == {"id": "plan-2"}

    assert len(calls) == 4
    assert waits == ["dev answered 409: A deployment is running."]


@pytest.mark.parametrize(
    "refusal",
    [
        httpx.Response(400, json={"error": "An agent can deploy only pull request builds."}),
        httpx.Response(401, json={"error": "Unauthorized"}),
        httpx.Response(403, json={"detail": "This Logos takes no deployments from an agent."}),
    ],
)
async def test_a_refusal_that_waiting_cannot_change_ends_the_order(monkeypatch, refusal):
    calls = fake_dev(monkeypatch, refusal)

    with pytest.raises(dev_deployments.DevDeploymentError) as refused:
        await dev_deployments.start("pr-93", poll_s=0)

    assert len(calls) == 1
    assert str(refusal.status_code) in str(refused.value)
    assert next(iter(refusal.json().values())) in str(refused.value)


async def test_an_order_gives_up_with_the_last_reason(monkeypatch):
    fake_dev(monkeypatch, BUSY)

    with pytest.raises(dev_deployments.DevDeploymentError, match="gave up.*A deployment is running"):
        await dev_deployments.start("pr-93", timeout_s=0.05, poll_s=0.01)


@pytest.mark.parametrize("core", ["succeeded", "unchanged"])
async def test_the_core_step_decides_while_the_workers_still_update(monkeypatch, core):
    calls = fake_dev(
        monkeypatch,
        deployment(core="running", worker="waiting"),
        # While the core is replaced: no connection, the orchestrator in the
        # place of the web service, and a page that is not the answer.
        httpx.ConnectError("connection refused"),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(200, text="<html></html>"),
        deployment(core=core, worker="running"),
    )

    status, _detail = await dev_deployments.wait_for_core("plan-1", poll_s=0)

    assert status == "success"
    assert len(calls) == 5
    assert calls[0]["url"] == f"{DEV}/api/deployments/agent/plans/plan-1"
    assert calls[0]["headers"] == {"Authorization": "Bearer key-for-dev"}


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (deployment(status="failed", core="failed", worker="cancelled", reason="dev-core: no health"), "no health"),
        (deployment(status="rolled_back", core="rolled_back", reason=None), "rolled_back"),
        # An admin abandoned the deployment while the core step ran.
        (deployment(status="failed", core="running", reason="Abandoned by Ada."), "Abandoned by Ada."),
    ],
)
async def test_a_core_that_does_not_end_well_fails_the_wait_with_the_reason(monkeypatch, answer, detail):
    fake_dev(monkeypatch, answer)

    status, said = await dev_deployments.wait_for_core("plan-1", poll_s=0)

    assert status == "failed"
    assert detail in said


async def test_a_core_step_that_does_not_end_times_out(monkeypatch):
    fake_dev(monkeypatch, deployment(core="running", worker="waiting"))

    status, said = await dev_deployments.wait_for_core("plan-1", timeout_s=0.05, poll_s=0.01)

    assert status == "timeout"
    assert "has not ended" in said
