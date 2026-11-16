"""Unit tests for streaming TTFT parsing in the overhead benchmark."""

from __future__ import annotations

import json
import time

import pytest
from run_benchmark import _sse_content_ttft_ns


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def iter_bytes(self):
        yield from self._chunks


def test_sse_ttft_ignores_schedule_comment_and_stamps_content() -> None:
    content = {
        "id": "x",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}],
    }
    body = (
        b": logos-schedule ettft=0.01\n\n"
        b": keepalive\n\n" + f"data: {json.dumps(content)}\n\n".encode() + b"data: [DONE]\n\n"
    )
    t0 = time.perf_counter_ns()
    ns = _sse_content_ttft_ns(_FakeStreamResponse([body]), t0)
    assert ns >= 0


def test_sse_ttft_rejects_error_event() -> None:
    err = {"error": {"message": "boom", "type": "server_error"}}
    body = f"data: {json.dumps(err)}\n\n".encode()
    with pytest.raises(RuntimeError, match="error event"):
        _sse_content_ttft_ns(_FakeStreamResponse([body]), time.perf_counter_ns())


def test_sse_ttft_rejects_comment_only_stream() -> None:
    body = b": logos-schedule ettft=0.01\n\n: keepalive\n\n"
    with pytest.raises(RuntimeError, match="no content delta"):
        _sse_content_ttft_ns(_FakeStreamResponse([body]), time.perf_counter_ns())
