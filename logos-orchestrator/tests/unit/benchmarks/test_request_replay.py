import asyncio
import json

import httpx
import pytest

from logos.benchmarks import replay
from logos.benchmarks.configuration import BenchmarkSettings


def response(reason="stop", usage=True, terminal=True):
    chunks = [{"choices": [{"delta": {"content": "Answer"}}]}, {"choices": [{"finish_reason": reason}]}]
    if usage:
        chunks.append({"choices": [], "usage": {"completion_tokens": 12}})
    return "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + ("data: [DONE]\n\n" if terminal else "")


@pytest.mark.parametrize(
    "reason,usage,terminal", [("length", True, True), ("stop", False, True), ("stop", True, False)]
)
async def test_truncated_or_unaccounted_streams_never_become_successes(reason, usage, terminal):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=response(reason, usage, terminal)))
    ) as client:
        with pytest.raises(RuntimeError):
            await replay.measure_request(client, "http://worker/v1/chat/completions", {}, {}, 9)


async def test_output_limit_error_identifies_the_source_and_stream_completion():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=response(reason="length")))
    ) as client:
        with pytest.raises(RuntimeError, match="source_log_id=9, finish_reason='length', stream_complete=True"):
            await replay.measure_request(client, "http://worker/v1/chat/completions", {}, {}, 9)


async def test_replay_sends_every_input_once_without_injecting_an_output_cap(monkeypatch):
    sent = []
    original_client = httpx.AsyncClient

    async def handler(request):
        sent.append(json.loads(request.content))
        await asyncio.sleep(0)
        return httpx.Response(200, text=response())

    monkeypatch.setattr(
        replay.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    snapshot = {
        "id": "a" * 64,
        "name": "Trial",
        "provenance": "synthetic",
        "request_count": 3,
        "requests": [
            {
                "source_log_id": index,
                "body": {"messages": [{"role": "user", "content": str(index)}], "temperature": 0.2},
            }
            for index in range(3)
        ],
    }
    summary = await replay.run_replay(
        snapshot=snapshot,
        target="http://worker/v1",
        model="model",
        api_key=None,
        request_headers={},
        settings=BenchmarkSettings(profile="concurrent", concurrency=2),
    )
    assert len(sent) == 3
    assert {body["messages"][0]["content"] for body in sent} == {"0", "1", "2"}
    assert all(body["stream_options"] == {"include_usage": True} and "max_tokens" not in body for body in sent)
    assert summary["metrics"]["request_totals"]["successful"] == 3
    assert summary["metrics"]["aggregate_output_tokens_per_second"] > 0
    assert [row["source_log_id"] for row in summary["metrics"]["replay_samples"]] == [0, 1, 2]


async def test_one_failure_cancels_and_awaits_sibling_streams(monkeypatch):
    sibling_started = asyncio.Event()
    cleaned = []

    async def measure(client, url, body, headers, source):
        if source == 0:
            await sibling_started.wait()
            raise RuntimeError("failed")
        try:
            sibling_started.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.append(source)

    monkeypatch.setattr(replay, "measure_request", measure)
    snapshot = {"request_count": 2, "requests": [{"source_log_id": i, "body": {}} for i in range(2)]}
    with pytest.raises(RuntimeError):
        await replay.run_replay(
            snapshot=snapshot,
            target="http://worker/v1",
            model="model",
            api_key=None,
            request_headers={},
            settings=BenchmarkSettings(profile="concurrent", concurrency=2),
        )
    assert cleaned == [1]


@pytest.mark.parametrize("reason, succeeds", [("end_turn", True), ("tool_use", True), ("max_tokens", False)])
async def test_messages_stream_uses_native_usage_and_preserves_inference_controls(reason, succeeds):
    sent = []
    events = [
        {"type": "message_start", "message": {"usage": {"output_tokens": 0}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Synthetic reasoning"}},
        {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"},
    ]

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    body = {
        "model": "model",
        "messages": [{"role": "user", "content": "Synthetic request"}],
        "system": "Synthetic system",
        "thinking": {"type": "adaptive"},
        "max_tokens": 8192,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if succeeds:
            measured = await replay.measure_request(
                client, "http://worker/v1/messages", body, {}, 7, operation="messages"
            )
            assert measured["output_tokens"] == 42
            assert measured["native_finish_reason"] == reason
            assert measured["ttft_ms"] is not None
        else:
            with pytest.raises(RuntimeError, match="incomplete"):
                await replay.measure_request(client, "http://worker/v1/messages", body, {}, 7, operation="messages")
    assert sent == [{**body, "stream": True}]
