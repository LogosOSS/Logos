"""Client cancellation leaves provider usage collection bounded and billable."""

import asyncio
import json
from contextlib import suppress

import pytest

from logos.live_stream import _StreamingLogAccumulator, _usage_tokens_from_payload
from logos.stream_drain import UsageDrainIterator, UsageDrainResponse


@pytest.mark.parametrize("cancel_read", [False, True])
async def test_disconnect_returns_before_late_usage_and_settles_once(cancel_read):
    accumulator = _StreamingLogAccumulator()
    release = asyncio.Event()
    closed = asyncio.Event()
    settled = []

    async def source():
        try:
            accumulator.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            yield b"first"
            await release.wait()
            accumulator.feed('data: {"usage":{"prompt_tokens":71,"completion_tokens":29}}\n\n')
            yield b"usage"
            await asyncio.Event().wait()
        finally:
            settled.append(accumulator.usage())
            closed.set()

    body = UsageDrainIterator(source(), accumulator, timeout=1)
    assert await anext(body) == b"first"
    if cancel_read:
        reader = asyncio.create_task(anext(body))
        await asyncio.sleep(0)
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader
    else:
        await body.aclose()
    assert not closed.is_set()
    assert settled == []
    release.set()
    await asyncio.wait_for(body.drain_task, 1)
    assert closed.is_set()
    assert settled == [{"prompt_tokens": 71, "completion_tokens": 29}]
    await body.aclose()
    assert len(settled) == 1


@pytest.mark.parametrize("ends", [False, True])
async def test_drain_timeout_or_eof_estimates_all_received_text(ends):
    accumulator = _StreamingLogAccumulator()
    settled = []
    request = {"messages": [{"role": "user", "content": "the input prompt"}]}

    async def source():
        try:
            for content in ["hello", " world!"]:
                accumulator.feed(f'data: {json.dumps({"choices": [{"delta": {"content": content}}]})}\n\n')
                yield content.encode()
            if not ends:
                await asyncio.Event().wait()
        finally:
            accumulator.finish()
            accumulator.estimate_disconnected_usage(request)
            settled.append(_usage_tokens_from_payload(accumulator.response_payload(), request))

    body = UsageDrainIterator(source(), accumulator, timeout=0.02)
    await anext(body)
    await body.aclose()
    await asyncio.wait_for(body.drain_task, 1)
    assert len(settled) == 1
    assert settled[0]["completion_tokens"] == 3
    assert settled[0]["prompt_tokens"] > 0
    assert settled[0]["usage_estimated"] == 1


async def test_asgi_send_failure_starts_drain_without_waiting_for_provider():
    accumulator = _StreamingLogAccumulator()
    release = asyncio.Event()

    async def source():
        yield b"first"
        await release.wait()
        accumulator.feed('data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n')
        yield b"usage"

    body = UsageDrainIterator(source(), accumulator, timeout=1)
    response = UsageDrainResponse(body)

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("Broken pipe")

    async def receive():
        await asyncio.Event().wait()

    with pytest.raises(Exception):
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 0.2)
    assert body.drain_task is not None
    assert not body.drain_task.done()
    release.set()
    await asyncio.wait_for(body.drain_task, 1)
    assert accumulator.usage()["completion_tokens"] == 2


def test_messages_initial_usage_does_not_prevent_estimation():
    accumulator = _StreamingLogAccumulator(client_disconnected=True)
    accumulator.feed('data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n')
    accumulator.feed('data: {"type":"content_block_delta","delta":{"thinking":"reasoning text"}}\n\n')
    assert not accumulator.has_final_usage()
    accumulator.estimate_disconnected_usage({"messages": [{"content": "hello"}]})
    assert accumulator.usage()["completion_tokens"] == 4
    assert accumulator.usage()["usage_estimated"] == 1


def test_usage_is_retained_after_a_later_usage_free_chunk():
    accumulator = _StreamingLogAccumulator(client_disconnected=True)
    accumulator.feed('data: {"usage":{"prompt_tokens":10,"completion_tokens":0}}\n\n')
    accumulator.feed('data: {"choices":[]}\n\n')
    accumulator.estimate_disconnected_usage({"messages": [{"content": "hello"}]})
    assert accumulator.usage() == {"prompt_tokens": 10, "completion_tokens": 0}


@pytest.mark.parametrize(
    "outcome,reported_usage,disconnect",
    [(outcome, False, True) for outcome in ("usage", "eof", "timeout", "error")]
    + [
        (outcome, reported, disconnected)
        for outcome in (
            "response.failed",
            "response.error",
            "error_event",
            "error_frame",
            "error_chunk",
            "anthropic_error",
        )
        for reported in (False, True)
        for disconnected in (False, True)
    ],
)
async def test_cloud_response_settles_using_provider_outcome(monkeypatch, outcome, reported_usage, disconnect):
    from types import SimpleNamespace

    from tests.unit.main.test_request_logging import _make_dummy_db, _make_pipeline

    import logos as main

    monkeypatch.setenv("LOGOS_STREAM_DRAIN_TIMEOUT_SECONDS", "0.05")
    released = asyncio.Event()
    upstream_closed = asyncio.Event()
    first = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'

    async def upstream(*args, **kwargs):
        if kwargs.get("on_headers"):
            kwargs["on_headers"]({"content-type": "text/event-stream"})
        try:
            yield first
            await released.wait()
            if outcome == "usage":
                yield b'data: {"usage":{"prompt_tokens":101,"completion_tokens":73,"total_tokens":174}}\n\n'
                await asyncio.Event().wait()
            elif outcome == "error":
                raise OSError("provider read failed")
            elif outcome == "timeout":
                await asyncio.Event().wait()
            elif outcome in {
                "response.failed",
                "response.error",
                "error_event",
                "error_frame",
                "error_chunk",
                "anthropic_error",
            }:
                usage = {"input_tokens": 101, "output_tokens": 73, "total_tokens": 174} if reported_usage else None
                if outcome.startswith("response."):
                    event = {"type": outcome, "response": {"error": {"message": "provider failed"}, "usage": usage}}
                elif outcome == "error_event":
                    event = {"type": "error", "message": "provider failed", "usage": usage}
                elif outcome == "anthropic_error":
                    event = {
                        "type": "error",
                        "error": {"type": "overloaded_error", "message": "provider failed"},
                        "usage": usage,
                    }
                elif outcome == "error_frame":
                    event = {"message": "provider failed", "usage": usage}
                else:
                    event = {"error": {"message": "provider failed"}, "usage": usage}
                prefix = "event: error\n" if outcome == "error_frame" else ""
                # No trailing newline: EOF must flush the final buffered data line
                # before settlement decides whether this generation succeeded.
                yield f"{prefix}data: {json.dumps(event)}".encode()
            else:
                yield b'data: {"choices":[{"delta":{"content":" world!"}}]}\n\n'
        finally:
            upstream_closed.set()

    db = _make_dummy_db()
    pipeline, completions, releases = _make_pipeline()
    pipeline.executor = SimpleNamespace(execute_streaming=upstream)
    monkeypatch.setattr(main, "_pipeline", pipeline)
    monkeypatch.setattr(main, "DBManager", db)
    monkeypatch.setattr(
        main,
        "_context_resolver",
        SimpleNamespace(prepare_headers_and_payload=lambda context, payload: ({}, payload)),
        raising=False,
    )
    response = await main._streaming_response(
        SimpleNamespace(
            provider_type="cloud",
            lane_id="lane",
            model_name="model",
            forward_url="http://provider",
            anthropic_dialect=None,
            messages_upstream=False,
        ),
        {"messages": [{"content": "the input prompt"}], "stream": True},
        42,
        12,
        27,
        -1,
        {},
        {"request_id": "drain-test", "provider_type": "cloud"},
    )
    body = response.body_iterator
    await anext(body)
    if disconnect:
        await body.aclose()
        assert not upstream_closed.is_set()
        assert completions == []
    released.set()
    if disconnect:
        await asyncio.wait_for(body.drain_task, 1)
    else:
        async for _ in body:
            pass
    assert upstream_closed.is_set()
    assert len(completions) == 1
    assert len(db.payload_calls) == 1
    usage = completions[0]["usage_tokens"]
    if outcome not in {"usage", "eof", "timeout"}:
        assert completions[0]["result_status"] == "error"
        assert "usage_estimated" not in usage
        assert "billed_requests" not in usage
        if reported_usage and outcome != "error":
            assert usage["prompt_tokens"] == 101
            assert usage["completion_tokens"] == 73
        else:
            assert not usage.get("prompt_tokens")
            assert not usage.get("completion_tokens")
    elif outcome == "usage":
        assert usage["prompt_tokens"] == 101
        assert usage["completion_tokens"] == 73
        assert "usage_estimated" not in usage
    elif disconnect:
        assert usage["usage_estimated"] == 1
        assert usage["prompt_tokens"] > 0
        assert usage["completion_tokens"] == (3 if outcome == "eof" else 2)


async def test_timeout_before_first_provider_byte_estimates_prompt_instead_of_assuming_empty():
    accumulator = _StreamingLogAccumulator()
    request = {"messages": [{"content": "a prompt the provider may still be processing"}]}
    usage = []

    async def source():
        try:
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            accumulator.estimate_disconnected_usage(request)
            usage.append(accumulator.usage())

    body = UsageDrainIterator(source(), accumulator, timeout=0.02)
    reader = asyncio.create_task(anext(body))
    await asyncio.sleep(0)
    reader.cancel()
    with suppress(asyncio.CancelledError):
        await reader
    await asyncio.wait_for(body.drain_task, 1)
    assert usage[0]["prompt_tokens"] > 0
    assert usage[0]["completion_tokens"] == 0
    assert usage[0]["usage_estimated"] == 1


async def test_usage_already_received_does_not_wait_for_an_outstanding_transport_read():
    accumulator = _StreamingLogAccumulator()
    closed = asyncio.Event()

    async def source():
        try:
            accumulator.feed('data: {"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n')
            yield b"usage"
            await asyncio.Event().wait()
        finally:
            closed.set()

    body = UsageDrainIterator(source(), accumulator, timeout=5)
    await anext(body)
    reader = asyncio.create_task(anext(body))
    await asyncio.sleep(0)
    reader.cancel()
    with suppress(asyncio.CancelledError):
        await reader
    await asyncio.wait_for(body.drain_task, 0.2)
    assert closed.is_set()
    assert not accumulator.drain_timed_out


async def test_split_error_frame_drains_usage_and_stops_without_waiting_for_eof():
    accumulator = _StreamingLogAccumulator()
    closed = asyncio.Event()

    async def source():
        try:
            accumulator.feed('data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
            yield b"first"
            accumulator.feed("event: error\n")
            yield b"header"
            accumulator.feed('data: {"message":"provider failed","usage":{"input_tokens":37,"output_tokens":19}}\n\n')
            yield b"error"
            await asyncio.Event().wait()
        finally:
            accumulator.finish()
            accumulator.estimate_disconnected_usage({"messages": [{"content": "prompt"}]})
            closed.set()

    body = UsageDrainIterator(source(), accumulator, timeout=5)
    await anext(body)
    await body.aclose()
    await asyncio.wait_for(body.drain_task, 0.2)
    assert closed.is_set()
    assert accumulator.upstream_error == {"message": "provider failed"}
    assert accumulator.usage() == {"input_tokens": 37, "output_tokens": 19}
    assert not accumulator.drain_timed_out
