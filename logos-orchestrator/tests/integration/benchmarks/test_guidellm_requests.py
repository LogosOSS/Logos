"""Exercise the real GuideLLM engine against a local SSE server, without a GPU."""

import copy
import importlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# The standalone adapter deliberately does not initialize the Logos application.
sys.path.insert(0, str(Path(__file__).parents[3] / "src/logos/benchmarks"))
engine = importlib.import_module("guidellm_requests")


def chat_events(reason="stop", tokens=12):
    return [
        {"choices": [{"index": 0, "delta": {"content": "First"}}]},
        {"choices": [{"index": 0, "delta": {"content": " second"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": tokens}},
    ]


def messages_events(reason="end_turn", tokens=12):
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Reasoning"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Answer"}},
        {"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": tokens}},
        {"type": "message_stop"},
    ]


@pytest.fixture
def server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, body, dict(self.headers)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = (
                messages_events()
                if self.path.endswith("/messages")
                else chat_events(reason="length" if self.path.startswith("/reject/") else "stop")
            )
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.01)
            if not self.path.endswith("/messages"):
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

    instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{instance.server_port}", received
    instance.shutdown()
    instance.server_close()
    thread.join()


@pytest.mark.asyncio
async def test_guidellm_measures_complete_frozen_requests_and_all_latency_metrics(server):
    target, received = server
    history = [
        {"role": "system", "content": "Synthetic instruction"},
        {"role": "user", "content": "Write a function"},
        {"role": "assistant", "content": "def add(a, b): return a + b"},
        {"role": "user", "content": "Add tests"},
    ]
    sources = [
        {"source_log_id": 1, "body": {"messages": history, "temperature": 0.2}},
        {
            "source_log_id": 2,
            "body": {
                "messages": history,
                "temperature": 0.7,
                "max_completion_tokens": 128,
                "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "stop": ["END"],
                "response_format": {"type": "json_object"},
            },
        },
        {
            "source_log_id": 3,
            "operation": "messages",
            "body": {
                "messages": [{"role": "user", "content": "Synthetic question"}],
                "system": "Synthetic system",
                "thinking": {"type": "adaptive"},
                "max_tokens": 8192,
            },
        },
    ]
    snapshot = {"id": "a" * 64, "request_count": 3, "requests": copy.deepcopy(sources)}
    report = await engine.measure_frozen_requests(
        snapshot=snapshot,
        target=target,
        model="test-model",
        api_key="test-only-key",
        request_headers={"x-logos-benchmark-token": "test-only-signature"},
        concurrency=2,
    )
    assert len(received) == 3
    for source in sources:
        expected = {**source["body"], "model": "test-model", "stream": True}
        operation = source.get("operation", "chat/completions")
        if operation == "chat/completions":
            expected["stream_options"] = {"include_usage": True}
        matches = [headers for path, body, headers in received if path == "/v1/" + operation and body == expected]
        assert len(matches) == 1
        assert matches[0]["Authorization"] == "Bearer test-only-key"
        assert matches[0]["x-logos-benchmark-token"] == "test-only-signature"
    assert snapshot["requests"] == sources
    metrics = report["benchmarks"][0]["metrics"]
    assert metrics["request_totals"] == {"total": 3, "successful": 3, "errored": 0, "incomplete": 0}
    for name in ("time_to_first_token_ms", "time_per_output_token_ms", "request_latency_ms"):
        dist = metrics[name]["successful"]
        assert 0 < dist["percentiles"]["p50"] <= dist["percentiles"]["p95"] <= dist["max"]
    assert metrics["aggregate_output_tokens_per_second"] == metrics["output_tokens_per_second"]["successful"]["mean"]
    assert [row["source_log_id"] for row in metrics["replay_samples"]] == [1, 2, 3]
    assert metrics["replay_samples"][2]["native_finish_reason"] == "end_turn"
    serialized = json.dumps(report)
    assert "Synthetic instruction" not in serialized and "test-only-key" not in serialized
    assert "test-only-signature" not in serialized and "Reasoning" not in serialized


@pytest.mark.asyncio
async def test_guidellm_rejects_a_cohort_with_a_truncated_response(server):
    target, received = server
    snapshot = {
        "id": "a" * 64,
        "request_count": 1,
        "requests": [
            {
                "source_log_id": 7,
                "body": {"messages": [{"role": "user", "content": "Synthetic question"}]},
            }
        ],
    }
    with pytest.raises(RuntimeError, match="did not complete every frozen input"):
        await engine.measure_frozen_requests(
            snapshot=snapshot,
            target=target + "/reject",
            model="test-model",
            api_key=None,
            request_headers={},
            concurrency=1,
        )
    assert len(received) == 1


@pytest.mark.parametrize("operation,reason", [("chat/completions", "length"), ("messages", "max_tokens")])
@pytest.mark.parametrize("limit", [None, True, 0, 11, 12, 13])
def test_only_exact_explicit_output_budgets_count_as_completed(operation, reason, limit):
    field = "max_tokens" if operation == "messages" else "max_completion_tokens"
    source = {"source_log_id": 9, "operation": operation, "body": {field: limit}}
    handler = engine.FrozenRequestHandler(source)
    events = messages_events(reason) if operation == "messages" else chat_events(reason)
    for event in events:
        handler.add_streaming_line("data: " + json.dumps(event))
    if operation == "chat/completions":
        handler.add_streaming_line("data: [DONE]")
    response = handler.compile_streaming(engine.GenerationRequest(), engine.GenerationRequestArguments())
    if type(limit) is int and limit == 12:
        handler.post_validation(response)
        assert response.response_metrics["finish_reason"] == "length"
    else:
        with pytest.raises(RuntimeError, match="source_log_id=9"):
            handler.post_validation(response)


@pytest.mark.parametrize("terminal,usage", [(False, True), (True, False)])
def test_incomplete_or_unaccounted_streams_are_rejected(terminal, usage):
    handler = engine.FrozenRequestHandler({"source_log_id": 4, "body": {}})
    for event in chat_events()[: None if usage else -1]:
        handler.add_streaming_line("data: " + json.dumps(event))
    if terminal:
        handler.add_streaming_line("data: [DONE]")
    response = handler.compile_streaming(engine.GenerationRequest(), engine.GenerationRequestArguments())
    with pytest.raises(RuntimeError):
        handler.post_validation(response)
