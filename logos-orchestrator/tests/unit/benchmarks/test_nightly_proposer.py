import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

import logos as gateway
from logos.benchmarks import nightly_proposer as proposer

OPTIONS = [{"max_num_seqs": 4}, {"max_num_seqs": 8}]
ROUTE_AND_EXECUTE = gateway.route_and_execute


def _reply(content):
    return JSONResponse({"choices": [{"message": {"content": content}}]})


@pytest.fixture
def inference(monkeypatch):
    registry = MagicMock()
    registry.active_provider_ids.return_value = [1, 2, 3, 4, 6, 5]
    registry.is_provider_online.return_value = True
    registry.has_received_first_status.return_value = True
    registry.is_calibrating.return_value = False
    registry.is_cordoned.return_value = False
    registry.peek_runtime_snapshot.side_effect = lambda provider_id: {
        "worker_id": "same-worker" if provider_id in (1, 2) else f"worker-{provider_id}",
        "runtime": {
            "devices": {"devices": [{"kind": "nvidia", "device_id": "GPU-shared"}]} if provider_id in (1, 6) else {},
            "lanes": [
                {
                    "model": proposer.PROPOSER_MODEL,
                    "runtime_state": "running",
                    "sleep_state": "sleeping" if provider_id == 4 else "awake",
                }
            ],
        },
    }
    monkeypatch.setattr(gateway, "_logosnode_registry", registry)
    monkeypatch.setattr(
        proposer.benchmark_reservations, "provider_owner", lambda provider_id: -42 if provider_id == 3 else None
    )
    deployments = [
        {"model_id": 90, "provider_id": provider_id, "type": "logosnode", "model_name": proposer.PROPOSER_MODEL}
        for provider_id in [1, 2, 3, 4, 6, 5]
    ]
    cloud = {**deployments[-1], "type": "cloud"}
    auth = MagicMock(api_key_id=51, team_id=None, user_id=None, environment="agent")
    db = MagicMock()
    db.__enter__.return_value = db
    db.log_usage.return_value = {"log-id": 123}, 200
    monkeypatch.setattr(proposer, "DBManager", MagicMock(return_value=db))
    monkeypatch.setattr(gateway, "_record_log_failure", MagicMock())
    monkeypatch.setattr(proposer, "_agent_access", MagicMock(return_value=(auth, [cloud, *deployments])))
    monkeypatch.setattr(
        gateway,
        "_filter_logosnode_deployments",
        AsyncMock(side_effect=lambda deployments, **kwargs: deployments),
    )
    call = AsyncMock(return_value=_reply('{"candidate_id": 1}'))
    monkeypatch.setattr(gateway, "route_and_execute", call)
    return call


async def test_uses_only_an_unreserved_separate_local_worker_and_aggregate_data(inference):
    index = await proposer.propose(
        excluded_worker_id=1,
        candidates=[*OPTIONS[:1], {**OPTIONS[1], "extra_args": ["private-value"]}],
        observations=[
            {
                "parameters": {"max_num_seqs": 6, "raw": "private-value"},
                "baseline": True,
                "throughput": 40,
                "p95_latency_ms": 31.0,
                "input_payload": "private-value",
            }
        ],
        limits={
            "gpu_count": 2,
            "gpu_memory_bytes": 50_000,
            "current": {"max_num_seqs": 6, "extra_args": ["private-value"]},
        },
        remaining_seconds=30,
    )
    assert index == 1
    request = inference.await_args.kwargs
    assert request["required_provider_id"] == 5
    assert request["deployments"] == [
        {"model_id": 90, "provider_id": 5, "type": "logosnode", "model_name": proposer.PROPOSER_MODEL}
    ]
    assert request["log_id"] == 123
    proposer.DBManager.return_value.log_usage.assert_called_once_with(
        api_key_id=51,
        team_id=None,
        user_id=None,
        environment="agent",
        log_level="BILLING",
        request_id=request["request_id"],
        timeout_s=proposer.PROPOSAL_TIMEOUT_SECONDS,
    )
    payload = request["body"]
    content = json.loads(payload["messages"][1]["content"])
    assert content["gpu_limits"] == {"gpu_count": 2, "gpu_memory_bytes": 50_000}
    assert content["baseline_parameters"] == {"max_num_seqs": 6}
    assert content["candidates"][1] == {"candidate_id": 1, "parameters": {"max_num_seqs": 8}}
    assert content["observations"] == [
        {"parameters": {"max_num_seqs": 6}, "baseline": True, "throughput": 40, "p95_latency_ms": 31.0}
    ]
    assert "private-value" not in json.dumps(payload)


async def test_falls_back_when_last_ready_worker_is_reserved(inference, monkeypatch):
    monkeypatch.setattr(
        proposer.benchmark_reservations, "provider_owner", lambda provider_id: -42 if provider_id in (3, 5) else None
    )
    assert (
        await proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30) is None
    )
    proposer._agent_access.assert_not_called()
    proposer.DBManager.assert_not_called()
    inference.assert_not_awaited()


@pytest.mark.parametrize(
    "content",
    [
        '{"candidate_id": -1}',
        '{"candidate_id": 2}',
        '{"candidate_id": true}',
        '{"candidate_id": "1"}',
        '{"candidate_id": 1, "parameters": {"max_num_seqs": 500}}',
        "reply without JSON",
    ],
)
async def test_invalid_response_uses_rule_fallback(inference, content):
    inference.return_value = _reply(content)
    assert (
        await proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30) is None
    )


async def test_timeout_cancels_inference(inference, monkeypatch):
    cancelled = asyncio.Event()
    monkeypatch.setattr(proposer, "PROPOSAL_TIMEOUT_SECONDS", 0.2)

    async def stall(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    inference.side_effect = stall
    assert (
        await proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30) is None
    )
    assert cancelled.is_set()
    request = inference.await_args.kwargs
    gateway._record_log_failure.assert_called_once_with(
        request["log_id"], request["request_id"], "Nightly proposal timed out.", result_status="timeout"
    )


async def test_usage_log_failure_skips_inference(inference):
    proposer.DBManager.return_value.log_usage.return_value = {"error": "unavailable"}, 503

    assert (
        await proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30) is None
    )
    inference.assert_not_awaited()


async def test_proposer_leaves_route_errors_to_terminal_usage_logging(inference, monkeypatch):
    # Execute the normal gateway wrapper to verify that failures use the same log row.
    monkeypatch.setattr(gateway, "route_and_execute", ROUTE_AND_EXECUTE)
    monkeypatch.setattr(gateway, "_answer_from_cache", AsyncMock(return_value=None))
    execute = AsyncMock(side_effect=HTTPException(status_code=503, detail="Model is unavailable"))
    monkeypatch.setattr(gateway, "_execute_proxy_mode", execute)

    assert (
        await proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30) is None
    )
    request = execute.await_args.kwargs
    gateway._record_log_failure.assert_called_once_with(
        request["log_id"], request["request_id"], "Model is unavailable", result_status="error"
    )


async def test_caller_cancellation_settles_usage_log(inference):
    started = asyncio.Event()

    async def stall(**kwargs):
        started.set()
        await asyncio.Future()

    inference.side_effect = stall
    task = asyncio.create_task(
        proposer.propose(excluded_worker_id=1, candidates=OPTIONS, observations=[], remaining_seconds=30)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    request = inference.await_args.kwargs
    gateway._record_log_failure.assert_called_once_with(
        request["log_id"], request["request_id"], "Nightly proposal cancelled.", result_status="error"
    )
