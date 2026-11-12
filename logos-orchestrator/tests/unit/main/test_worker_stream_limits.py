"""Worker ingress limits fail streams without blocking the session reader."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import StreamingResponse
from tests.unit.main.test_stream_keepalive import _Client, _collect

import logos as main
from logos.logosnode_registry import (
    LogosNodeCommandError,
    LogosNodeOfflineError,
    LogosNodeRuntimeRegistry,
    ProviderSession,
)


@pytest.fixture
def worker(monkeypatch):
    for name, value in {
        "LOGOSNODE_STREAM_MAX_CHUNKS": 2,
        "LOGOSNODE_STREAM_MAX_BYTES": 8,
        "LOGOSNODE_STREAM_MAX_CHUNK_BYTES": 4,
        "LOGOSNODE_SESSION_MAX_STREAM_BYTES": 16,
    }.items():
        monkeypatch.setenv(name, str(value))
    registry = LogosNodeRuntimeRegistry()
    websocket = AsyncMock()
    session = ProviderSession(provider_id=7, worker_id="worker-a", websocket=websocket)
    registry._sessions[7] = session
    return registry, session


async def _chunk(registry, cmd_id, chunk):
    await asyncio.wait_for(
        registry.on_stream_chunk(7, {"cmd_id": cmd_id, "chunk_b64": base64.b64encode(chunk).decode("ascii")}),
        timeout=1,
    )


async def _start(worker):
    registry, session = worker
    stream = registry.send_stream_command(7, "infer_stream")
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    cmd_id = session.websocket.send_json.call_args.args[0]["cmd_id"]
    await _chunk(registry, cmd_id, b"init")
    assert await asyncio.wait_for(pending, 1) == b"init"
    assert session.stream_budget.buffered_bytes == sum(b.buffered_bytes for b in session.pending_streams.values())
    return stream, cmd_id, session.pending_streams[cmd_id]


@pytest.mark.parametrize(
    "chunks,limit,reason",
    [
        ([b"", b"", b""], None, "stream chunk count"),
        ([b"abc", b"de"], ("max_stream_bytes", 4), "stream byte"),
        ([b"12345"], None, "chunk byte"),
    ],
    ids=["count", "bytes", "oversized-chunk"],
)
async def test_overflow_fails_stream_closes_session_and_releases_buffers(worker, caplog, chunks, limit, reason):
    registry, session = worker
    stream, cmd_id, buffer = await _start(worker)
    if limit:
        setattr(session.stream_budget, *limit)
    for chunk in chunks:
        await _chunk(registry, cmd_id, chunk)

    session.websocket.close.assert_awaited_once()
    assert 7 not in registry._sessions
    assert session.pending_streams == {}
    assert buffer.buffered_bytes == session.stream_budget.buffered_bytes == 0
    assert "Closing worker session: provider=7 worker=worker-a" in caplog.text
    assert cmd_id in caplog.text
    assert reason in caplog.text
    with pytest.raises(LogosNodeCommandError, match=reason):
        await asyncio.wait_for(anext(stream), 1)
    # Already queued WebSocket frames cannot resurrect a closed stream.
    await _chunk(registry, cmd_id, b"late")
    assert session.stream_budget.buffered_bytes == 0


async def test_oversized_encoded_chunk_is_rejected_before_decoding(worker, monkeypatch):
    registry, session = worker
    stream, cmd_id, _ = await _start(worker)

    def unexpected_decode(_):
        pytest.fail("oversized frame must be rejected before decoding")

    monkeypatch.setattr(base64, "b64decode", unexpected_decode)
    await _chunk(registry, cmd_id, b"1234567")
    with pytest.raises(LogosNodeCommandError, match="chunk byte"):
        await anext(stream)
    session.websocket.close.assert_awaited_once()


async def test_session_byte_limit_is_shared_across_streams(worker):
    registry, session = worker
    session.stream_budget.max_session_bytes = 8
    streams = [await _start(worker) for _ in range(3)]
    other = ProviderSession(provider_id=8, worker_id="worker-b", websocket=AsyncMock())
    registry._sessions[8] = other

    for _, cmd_id, _ in streams[:2]:
        await _chunk(registry, cmd_id, b"123")
    assert session.stream_budget.buffered_bytes == 6
    await _chunk(registry, streams[2][1], b"123")

    for stream, _, buffer in streams:
        with pytest.raises(LogosNodeCommandError, match="session stream byte"):
            await asyncio.wait_for(anext(stream), 1)
        assert buffer.buffered_bytes == 0
    assert session.stream_budget.buffered_bytes == 0
    session.websocket.close.assert_awaited_once()
    assert registry._sessions[8] is other
    other.websocket.close.assert_not_awaited()


async def test_normal_stream_at_limits_preserves_bytes_order_and_terminal_result(worker):
    registry, session = worker
    session.stream_budget.max_session_bytes = 8
    stream, cmd_id, buffer = await _start(worker)
    for _ in range(3):
        for _ in range(10):
            await registry.on_stream_start(7, {"cmd_id": cmd_id, "headers": {"content-type": "text/event-stream"}})
        await _chunk(registry, cmd_id, b"abcd")
        await _chunk(registry, cmd_id, b"efgh")
        assert buffer.buffered_bytes == session.stream_budget.buffered_bytes == 8
        assert await anext(stream) == b"abcd"
        assert await anext(stream) == b"efgh"
        assert buffer.buffered_bytes == session.stream_budget.buffered_bytes == 0

    await _chunk(registry, cmd_id, b"last")
    await _chunk(registry, cmd_id, b"done")
    for _ in range(10):
        await registry.on_stream_end(7, {"cmd_id": cmd_id, "success": True})
    await _chunk(registry, cmd_id, b"late")
    assert [chunk async for chunk in stream] == [b"last", b"done"]
    assert session.pending_streams == {}
    assert session.stream_budget.buffered_bytes == 0
    session.websocket.close.assert_not_awaited()


@pytest.mark.parametrize("disconnect", [False, True], ids=["consumer-close", "worker-disconnect"])
async def test_abandoned_buffers_release_session_budget(worker, disconnect):
    registry, session = worker
    stream, cmd_id, buffer = await _start(worker)
    await _chunk(registry, cmd_id, b"abcd")
    await _chunk(registry, cmd_id, b"efgh")
    if disconnect:
        await registry.detach_session(7, session.websocket)
        with pytest.raises(LogosNodeCommandError, match="Worker disconnected"):
            await asyncio.wait_for(anext(stream), 1)
    else:
        await stream.aclose()
    assert buffer.buffered_bytes == session.stream_budget.buffered_bytes == 0
    assert session.pending_streams == {}


async def test_overflow_before_first_chunk_wakes_waiting_consumer(worker):
    registry, session = worker
    stream = registry.send_stream_command(7, "infer_stream")
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    cmd_id = session.websocket.send_json.call_args.args[0]["cmd_id"]
    await _chunk(registry, cmd_id, b"12345")
    with pytest.raises(LogosNodeCommandError, match="chunk byte"):
        await asyncio.wait_for(pending, 1)
    assert session.stream_budget.buffered_bytes == 0


async def test_completed_stream_remains_readable_after_worker_disconnect(worker):
    registry, session = worker
    stream, cmd_id, _ = await _start(worker)
    await _chunk(registry, cmd_id, b"last")
    await registry.on_stream_end(7, {"cmd_id": cmd_id, "success": True})
    await registry.detach_session(7, session.websocket)
    assert [chunk async for chunk in stream] == [b"last"]
    assert session.stream_budget.buffered_bytes == 0


@pytest.mark.parametrize("cancelled", [False, True], ids=["send-error", "send-cancelled"])
async def test_send_interruption_releases_bytes_received_during_dispatch(worker, cancelled):
    registry, session = worker

    async def send(message):
        await registry.on_stream_chunk(7, {"cmd_id": message["cmd_id"], "chunk_b64": "YWJj"})
        if cancelled:
            raise asyncio.CancelledError
        raise OSError("connection lost")

    session.websocket.send_json.side_effect = send
    stream = registry.send_stream_command(7, "infer_stream")
    with pytest.raises(asyncio.CancelledError if cancelled else LogosNodeOfflineError):
        await anext(stream)
    assert session.pending_streams == {}
    assert session.stream_budget.buffered_bytes == 0


async def test_overflow_reaches_client_as_upstream_error_frame(worker, monkeypatch):
    registry, session = worker
    stream, cmd_id, _ = await _start(worker)
    await _chunk(registry, cmd_id, b"12345")

    async def route(**kwargs):
        return StreamingResponse(stream, media_type="text/event-stream")

    monkeypatch.setattr(main, "route_and_execute", route)
    response = await main._keepalive_streaming_response(
        _Client(leaves=False), log_id=1, request_id="req-overflow", path="chat/completions"
    )
    raw = await asyncio.wait_for(_collect(response), 1)
    errors = [json.loads(line[6:])["error"] for line in raw.splitlines() if line.startswith(b'data: {"error"')]
    assert len(errors) == 1
    assert errors[0]["type"] == "api_error"
    assert "Worker stream buffer overflow" in errors[0]["message"]
    assert b"data: [DONE]" in raw
