import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.responses import JSONResponse

import logos as gateway
from logos.benchmarks import nightly_proposer as proposer

OPTIONS = [{"max_num_seqs": 4}, {"max_num_seqs": 8}]


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
    auth = MagicMock()
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
    assert request["log_id"] is None
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
