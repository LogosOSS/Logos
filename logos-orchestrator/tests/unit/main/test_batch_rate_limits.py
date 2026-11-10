"""Local batches share HTTP admission and token accounting, and wait on limits."""

import asyncio
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import logos as main
from logos import batch_local
from logos.auth import initialize_rate_limits

rate_limiter = importlib.import_module("logos.rate_limiter")


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "logos.main", main)
    key = {
        "id": 11,
        "key_value": "lg-test",
        "name": "limited",
        "key_type": "application",
        "team_id": 7,
        "user_id": None,
        "environment": "test",
        "settings": {"rpm_limit": 1, "tpm_limit": 10},
    }
    db = MagicMock()
    db.__enter__.return_value = db
    db.get_api_key_by_id.return_value = key
    db.get_team.return_value = {}
    db.log_usage.return_value = ({"log-id": 100}, 200)
    db.resolve_proxy_model.return_value = (1, "test-model")
    db.get_local_batch_lines.return_value = {}
    db.update_local_batch_progress.return_value = False
    db.finish_local_batch.return_value = True
    monkeypatch.setattr(main, "DBManager", lambda: db)
    monkeypatch.setattr(batch_local, "DBManager", lambda: db)
    deployments = [{"model_id": 1, "provider_id": 2, "type": "logosnode", "model_name": "test-model"}]
    monkeypatch.setattr(main, "request_setup", lambda *args, **kwargs: (deployments, ["test-model"]))
    monkeypatch.setattr(main, "_filter_logosnode_deployments", AsyncMock(return_value=deployments))
    monkeypatch.setattr(main, "_extract_policy", lambda *args: {})
    context = SimpleNamespace(
        provider_type="logosnode", lane_id="lane", model_name="test-model", anthropic_dialect=None
    )
    pipeline = MagicMock()

    async def schedule(request):
        return SimpleNamespace(
            success=True,
            model_id=1,
            provider_id=2,
            execution_context=context,
            classification_stats={},
            scheduling_stats={"provider_type": "logosnode", "request_id": request.request_id},
        )

    pipeline.process = AsyncMock(side_effect=schedule)
    monkeypatch.setattr(main, "_pipeline", pipeline)
    monkeypatch.setattr(
        main,
        "_context_resolver",
        SimpleNamespace(prepare_headers_and_payload=lambda context, payload: ({}, payload)),
        raising=False,
    )
    infer = AsyncMock(
        return_value={
            "status_code": 200,
            "body": {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 5}},
        }
    )
    monkeypatch.setattr(main, "_logosnode_registry", SimpleNamespace(send_command=infer))
    now = [100.0]
    monkeypatch.setattr(rate_limiter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    limiter = rate_limiter.InMemoryRateLimiter()
    monkeypatch.setattr(rate_limiter, "_rate_limiter", limiter)
    auth = batch_local.auth_context_for_key(11)
    line = {"custom_id": "one", "url": "/v1/chat/completions", "body": {"model": "test-model", "messages": []}}
    return SimpleNamespace(db=db, auth=auth, line=line, limiter=limiter, now=now, infer=infer, pipeline=pipeline)


@pytest.mark.parametrize("settings", [{"rpm_limit": 1, "tpm_limit": 10}, {}])
async def test_http_and_job_initialize_the_same_effective_limits(runtime, monkeypatch, settings):
    runtime.auth.settings = settings
    runtime.db.get_team.return_value = {"default_local_rpm_limit": 1, "default_local_tpm_limit": 10}
    monkeypatch.setattr(main, "authenticate_api_key", lambda *args, **kwargs: runtime.auth)
    runtime.db.get_deployments_for_api_key.return_value = []
    payload = json.dumps(runtime.line["body"]).encode()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        },
        receive=AsyncMock(return_value={"type": "http.request", "body": payload}),
    )
    await main.auth_parse_log(request, use_profile_auth=True)
    http_limits = runtime.auth.local_rl.copy()
    runtime.auth.local_rl = None
    await main.execute_proxy_job("v1/chat/completions", {}, runtime.line["body"], None, runtime.auth, 100)
    assert runtime.auth.local_rl == http_limits == {"rpm": 1, "tpm": 10}


def test_effective_limits_prefer_scope_then_key_then_team(runtime):
    runtime.auth.settings = {"rpm_limit": 1, "local_tpm_limit": 10}
    initialize_rate_limits(
        runtime.auth,
        {"default_local_rpm_limit": 20, "default_local_tpm_limit": 30, "default_cloud_tpm_limit": 40},
    )
    assert runtime.auth.local_rl == {"rpm": 1, "tpm": 10}
    assert runtime.auth.cloud_rl == {"rpm": 1, "tpm": 40}
    runtime.auth.settings = {}
    initialize_rate_limits(runtime.auth, None)
    assert runtime.auth.local_rl is None
    assert runtime.auth.cloud_rl is None


@pytest.mark.parametrize("tokens, reason", [(1, "RPM"), (10, "TPM")])
async def test_local_batch_line_waits_for_shared_rpm_and_tpm_window(runtime, monkeypatch, tokens, reason):
    runtime.infer.return_value["body"]["usage"] = {"prompt_tokens": tokens, "completion_tokens": 0}
    first, failed = await batch_local._run_line(runtime.line, runtime.auth, {})
    assert not failed
    assert first["response"]["status_code"] == 200
    bucket = "api_key:11:local"
    assert sum(count for _, count in runtime.limiter._token_windows[bucket]) == tokens

    # HTTP traffic of the same key sees the batch line's request and tokens.
    with pytest.raises(HTTPException) as exc:
        await main.route_and_execute(
            deployments=[{"model_id": 1, "provider_id": 2, "model_name": "test-model"}],
            body=runtime.line["body"],
            headers={},
            auth=runtime.auth,
            path="v1/chat/completions",
            log_id=100,
        )
    assert exc.value.status_code == 429
    assert reason in exc.value.detail
    assert exc.value.headers["Retry-After"] == "60"

    delays = []

    async def advance(delay):
        delays.append(delay)
        assert runtime.infer.await_count == 1
        runtime.now[0] += delay + 0.001

    monkeypatch.setattr(batch_local.asyncio, "sleep", advance)
    second, failed = await batch_local._run_line({**runtime.line, "custom_id": "two"}, runtime.auth, {})
    assert not failed
    assert second["response"]["status_code"] == 200
    assert delays == [60.0]
    assert runtime.infer.await_count == 2
    assert len(runtime.limiter._request_windows[bucket]) == 1
    assert sum(count for _, count in runtime.limiter._token_windows[bucket]) == tokens
    assert runtime.pipeline.scheduler.release.call_count == 4


async def test_batch_cancellation_stops_waiting_without_checkpointing_a_failure(runtime, monkeypatch):
    await batch_local._run_line(runtime.line, runtime.auth, {})
    runtime.db.update_local_batch_progress.side_effect = [False, True]
    monkeypatch.setattr(batch_local, "LOCAL_BATCH_LEASE_TTL_S", 0.03)
    result = await asyncio.wait_for(
        batch_local._execute_lines(20, {"upstream_id": "batch_test", "api_key_id": 11}, [runtime.line], runtime.auth),
        timeout=2,
    )
    assert result.startswith("file-")
    assert runtime.infer.await_count == 1
    assert runtime.pipeline.process.await_count == 2
    runtime.db.save_local_batch_lines.assert_not_called()
    assert runtime.db.finish_local_batch.call_args.kwargs["status"] == "cancelled"
    assert runtime.db.finish_local_batch.call_args.kwargs["failed"] == 0


async def test_runner_task_cancellation_stops_rate_limit_wait(runtime):
    await batch_local._run_line(runtime.line, runtime.auth, {})
    task = asyncio.create_task(batch_local._run_line(runtime.line, runtime.auth, {}))
    await asyncio.sleep(0)
    assert runtime.pipeline.process.await_count == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.infer.await_count == 1


@pytest.mark.parametrize("retry_after", [None, "invalid", "0", "nan"])
async def test_missing_or_invalid_retry_after_uses_bounded_backoff(runtime, monkeypatch, retry_after):
    execute = AsyncMock(
        side_effect=[{"status_code": 429, "headers": {"Retry-After": retry_after}}] * 8
        + [{"status_code": 200, "data": {}}]
    )
    monkeypatch.setattr(main, "execute_proxy_job", execute)
    sleep = AsyncMock()
    monkeypatch.setattr(batch_local.asyncio, "sleep", sleep)
    _, failed = await batch_local._run_line(runtime.line, runtime.auth, {})
    assert not failed
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4, 8, 16, 32, 60, 60]


@pytest.mark.parametrize("remaining_seconds", [-1, 0.1])
async def test_batch_completion_window_bounds_rate_limit_wait(runtime, remaining_seconds):
    await batch_local._run_line(runtime.line, runtime.auth, {})
    batch = {
        "upstream_id": "batch_test",
        "api_key_id": 11,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=24) + timedelta(seconds=remaining_seconds),
        "completion_window": "24h",
    }
    result = await asyncio.wait_for(batch_local._execute_lines(20, batch, [runtime.line], runtime.auth), timeout=2)
    assert result.startswith("file-")
    assert runtime.infer.await_count == 1
    assert runtime.pipeline.process.await_count == (2 if remaining_seconds > 0 else 1)
    runtime.db.save_local_batch_lines.assert_not_called()
    assert runtime.db.finish_local_batch.call_args.kwargs["status"] == "expired"
    assert runtime.db.finish_local_batch.call_args.kwargs["failed"] == 0
