from unittest.mock import MagicMock

import pytest

import logos as main


async def test_route_and_execute_no_deployments_sync():
    """route_and_execute raises 404 when deployments list is empty."""
    with pytest.raises(main.HTTPException) as exc:
        await main.route_and_execute(
            deployments=[],
            body={},
            headers={},
            auth=MagicMock(key_value="lg-key"),
            path="chat/completions",
            log_id=None,
            is_async_job=False,
        )
    assert exc.value.status_code == 404


async def test_route_and_execute_no_deployments_async():
    """route_and_execute returns error dict when deployments empty in async mode."""
    result = await main.route_and_execute(
        deployments=[],
        body={},
        headers={},
        auth=MagicMock(key_value="lg-key"),
        path="chat/completions",
        log_id=None,
        is_async_job=True,
    )
    assert result["status_code"] == 404
    assert "error" in result["data"]


async def test_route_and_execute_proxy_branch(monkeypatch):
    """route_and_execute delegates to _execute_proxy_mode when body has 'model'."""
    called = {}

    async def fake_proxy(
        body,
        headers,
        auth,
        deployments,
        log_id,
        is_async_job,
        request_id=None,
        request_path=None,
        priority=1,
        required_provider_id=None,
    ):
        called["proxy"] = True
        return {"status": "proxy"}

    monkeypatch.setattr(main, "_execute_proxy_mode", fake_proxy)

    out = await main.route_and_execute(
        deployments=[{"model_id": 1, "provider_id": 1}],
        body={"model": "gpt-4o"},
        headers={"h": "v"},
        auth=MagicMock(key_value="lg-key"),
        path="chat/completions",
        log_id=1,
        is_async_job=False,
    )
    assert out == {"status": "proxy"}
    assert called.get("proxy") is True


async def test_route_and_execute_resource_branch(monkeypatch):
    """route_and_execute delegates to _execute_resource_mode when body has no 'model'."""
    called = {}

    async def fake_resource(deployments, body, headers, auth, log_id, is_async_job, **kw):
        called["resource"] = True
        return {"status": "resource"}

    monkeypatch.setattr(main, "_execute_resource_mode", fake_resource)

    out = await main.route_and_execute(
        deployments=[{"model_id": 1, "provider_id": 1}],
        body={},
        headers={"h": "v"},
        auth=MagicMock(key_value="lg-key"),
        path="chat/completions",
        log_id=1,
        is_async_job=True,
    )
    assert out == {"status": "resource"}
    assert called.get("resource") is True


IMAGE_BODY = {
    "model": "gpt-5.6-luna",
    "input": "draw a lighthouse",
    "tools": [{"type": "image_generation", "model": "gpt-image-1.5"}],
}


async def test_an_image_tool_model_the_key_may_not_use_is_refused_before_scheduling(monkeypatch):
    async def must_not_run(**kwargs):
        raise AssertionError("the request must not be scheduled")

    monkeypatch.setattr(main, "_execute_proxy_mode", must_not_run)

    with pytest.raises(main.HTTPException) as exc:
        await main.route_and_execute(
            deployments=[{"model_id": 1, "provider_id": 8, "model_name": "gpt-5.6-luna"}],
            body=IMAGE_BODY,
            headers={},
            auth=MagicMock(key_value="lg-key"),
            path="responses",
            log_id=None,
            is_async_job=False,
        )
    assert exc.value.status_code == 403
    assert "gpt-image-1.5" in exc.value.detail

    result = await main.route_and_execute(
        deployments=[{"model_id": 1, "provider_id": 8, "model_name": "gpt-5.6-luna"}],
        body=IMAGE_BODY,
        headers={},
        auth=MagicMock(key_value="lg-key"),
        path="responses",
        log_id=None,
        is_async_job=True,
    )
    assert result["status_code"] == 403


async def test_an_image_tool_model_the_key_may_use_is_scheduled(monkeypatch):
    async def fake_proxy(**kwargs):
        return {"status": "proxy"}

    monkeypatch.setattr(main, "_execute_proxy_mode", fake_proxy)

    out = await main.route_and_execute(
        deployments=[
            {"model_id": 1, "provider_id": 8, "model_name": "gpt-5.6-luna"},
            {"model_id": 2, "provider_id": 8, "model_name": "gpt-image-1.5"},
        ],
        body=IMAGE_BODY,
        headers={},
        auth=MagicMock(key_value="lg-key"),
        path="responses",
        log_id=None,
        is_async_job=False,
    )
    assert out == {"status": "proxy"}
