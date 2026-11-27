"""Asks the Logos on dev to deploy the build of a pull request.

A Logos deploys only itself, so this runner orders the deployment from the
Logos on dev and follows it there. It authenticates with the agent deployment
key (LOGOS_AGENT_DEV_DEPLOYMENT_KEY here, LOGOS_DEPLOYMENTS_AGENT_KEY on dev),
which stays in this service: session containers never see it. A Logos accepts
that key only for pull request builds, and never if it is protected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from .config import settings

logger = logging.getLogger(__name__)

# A Logos runs one deployment at a time. One that is stuck ends after the
# silence limit of 30 minutes, so an order waits a little longer than that.
ORDER_TIMEOUT_S = 40 * 60
# The longest wait for the core to run the new build.
CORE_TIMEOUT_S = 20 * 60
POLL_S = 15.0

# Answers that the same order cannot change: a bad tag, a wrong key, or a
# Logos that takes no orders from an agent. Anything else can pass, because
# another deployment runs or the core is being replaced.
_FINAL_REFUSALS = frozenset({400, 401, 403})

# The states of a step that has not ended, and of one that ended well.
_OPEN_STEPS = frozenset({"waiting", "ordered", "running"})
_GOOD_STEPS = frozenset({"succeeded", "unchanged"})


class DevDeploymentError(RuntimeError):
    """The Logos on dev did not take the order."""


def missing_setting() -> str | None:
    """Why this runner cannot deploy to dev, or None if it can."""
    if not settings.deploy_enabled:
        return "Deploys are disabled on this runner (LOGOS_AGENT_DEPLOY_ENABLED)"
    if not settings.dev_deployment_key:
        return "Deploys need the key for dev on this runner (LOGOS_AGENT_DEV_DEPLOYMENT_KEY)"
    return None


def page_url(plan_id: str) -> str:
    """The page of a deployment on dev, for people."""
    return f"{settings.dev_base_url.rstrip('/')}/deployments/{plan_id}"


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    url = f"{settings.dev_base_url.rstrip('/')}/api/deployments/agent{path}"
    headers = {"Authorization": f"Bearer {settings.dev_deployment_key}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await client.request(method, url, headers=headers, **kwargs)


def _refusal(response: httpx.Response) -> str:
    """What dev answered, in the words of the Logos there if it sent any."""
    try:
        body = response.json()
        said = body.get("detail") or body.get("error")
    except (ValueError, AttributeError):
        said = None
    return f"dev answered {response.status_code}: {said or response.text[:200]}"


async def start(
    image_tag: str,
    *,
    on_wait: Callable[[str], Awaitable[None]] | None = None,
    timeout_s: float = ORDER_TIMEOUT_S,
    poll_s: float = POLL_S,
) -> dict:
    """Order the deployment of ``image_tag`` and return the new deployment.

    While another deployment runs on dev, or dev does not answer, the order is
    repeated until ``timeout_s`` has passed. ``on_wait`` hears the reason of
    the first wait. Raises :class:`DevDeploymentError`.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    waited = False
    while True:
        try:
            response = await _request("POST", "/plans", json={"tag": image_tag})
        except httpx.HTTPError as exc:
            problem = f"could not reach dev: {exc!r}"
        else:
            if response.status_code == 201:
                return response.json()
            problem = _refusal(response)
            if response.status_code in _FINAL_REFUSALS:
                raise DevDeploymentError(problem)
        if loop.time() >= deadline:
            raise DevDeploymentError(f"gave up after {timeout_s / 60:.0f} min; {problem}")
        if on_wait and not waited:
            await on_wait(problem)
        waited = True
        await asyncio.sleep(poll_s)


async def wait_for_core(
    plan_id: str,
    *,
    timeout_s: float = CORE_TIMEOUT_S,
    poll_s: float = POLL_S,
) -> tuple[str, str]:
    """Wait until the core step of a deployment has ended.

    The core holds the services and the web application, so the new build shows
    once its step ended well; the worker nodes may still be updating then.
    While the core is replaced, dev does not answer, or another service answers
    in its place. No such answer is final.

    Returns ``(status, detail)``: status is ``"success"``, ``"failed"``, or
    ``"timeout"``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    seen = "no answer yet"
    while True:
        try:
            response = await _request("GET", f"/plans/{plan_id}")
            if response.status_code != 200:
                seen = _refusal(response)
            else:
                plan = response.json()
                core = [step["status"] for step in plan["steps"] if step["kind"] == "core"]
                if core and not _OPEN_STEPS.intersection(core):
                    if _GOOD_STEPS.issuperset(core):
                        return "success", "the core runs the new build"
                    return "failed", plan.get("reason") or f"the core step ended as {core[0]}"
                if plan["status"] != "running":
                    return "failed", plan.get("reason") or f"the deployment ended as {plan['status']}"
                seen = "the core step has not ended"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            seen = f"no usable answer from dev: {exc!r}"
        if loop.time() >= deadline:
            logger.warning("deployment %s on dev did not end in time: %s", plan_id, seen)
            return "timeout", f"not done after {timeout_s / 60:.0f} min; {seen}"
        await asyncio.sleep(poll_s)
