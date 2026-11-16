"""Mock vLLM lane for the per-request overhead benchmark.

Serves the endpoints the worker's LaneManager probes and relays to, with
fixed (deterministic) responses so measured latency is dominated by the
Logos forwarding path rather than generation:

- ``GET /health``                     → 200 {}
- ``GET /v1/models``                  → fixed model list (LaneManager liveness probe)
- ``GET /metrics``                    → Prometheus text; token counters *do*
                                         increase per request so the worker's
                                         stuck-lane detection stays quiet
- ``GET /is_sleeping``                → {"is_sleeping": false}; optional artificial
                                         delay via ``LOGOS_BENCH_IS_SLEEPING_DELAY_MS``
                                         (regression for issue #165 — acquire must
                                         not await this on the infer hot path)
- ``POST /v1/chat/completions``       → fixed ~1.5 KB OpenAI response, or SSE
                                         when ``stream=true`` (first content chunk
                                         is immediate so client TTFT isolates the
                                         Logos path)

Run: ``uvicorn mock_lane:app --host 127.0.0.1 --port <lane-port> --no-access-log``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

_E2E = Path(__file__).parents[2] / "e2e"
if str(_E2E) not in sys.path:
    sys.path.insert(0, str(_E2E))

from harness.fake_vllm.protocol import chat_completion_payload, models_payload  # noqa: E402

MODEL_NAME = os.environ.get("LOGOS_BENCH_MODEL", "bench-local-model")
try:
    _IS_SLEEPING_DELAY_S = max(0.0, float(os.environ.get("LOGOS_BENCH_IS_SLEEPING_DELAY_MS", "0") or "0") / 1000.0)
except (TypeError, ValueError):
    _IS_SLEEPING_DELAY_S = 0.0

app = FastAPI()

# Deterministic completion content sized to a realistic ~1.5 KB JSON body.
_FILLER = "The quick brown fox jumps over the lazy dog. " * 28  # ≈1.5 KB
_USAGE = {"prompt_tokens": 12, "completion_tokens": 300, "total_tokens": 312}

# Metrics counters (incremented per request — stuck-lane detection compares them).
_prompt_tokens_total = 0
_completion_tokens_total = 0
_is_sleeping_hits = 0
_start = time.monotonic()


class _ChatRequest(BaseModel):
    model: str = ""
    messages: list = []
    stream: bool = False
    stream_options: dict | None = None


def _bump_usage() -> str:
    global _prompt_tokens_total, _completion_tokens_total
    _prompt_tokens_total += _USAGE["prompt_tokens"]
    _completion_tokens_total += _USAGE["completion_tokens"]
    return f"chatcmpl-bench-{_prompt_tokens_total}"


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {}


@app.get("/v1/models")
async def models() -> Dict[str, Any]:
    return models_payload(MODEL_NAME, owned_by="bench", created=1700000000)


@app.get("/is_sleeping")
async def is_sleeping() -> Dict[str, Any]:
    global _is_sleeping_hits
    _is_sleeping_hits += 1
    if _IS_SLEEPING_DELAY_S > 0:
        await asyncio.sleep(_IS_SLEEPING_DELAY_S)
    return {"is_sleeping": False}


@app.get("/_bench/is_sleeping_hits")
async def is_sleeping_hits() -> Dict[str, Any]:
    """Test-only counter: how often /is_sleeping was awaited."""
    return {"hits": _is_sleeping_hits, "delay_ms": int(_IS_SLEEPING_DELAY_S * 1000)}


@app.post("/_bench/reset_is_sleeping_hits")
async def reset_is_sleeping_hits() -> Dict[str, Any]:
    """Clear the probe counter between warmup and measurement."""
    global _is_sleeping_hits
    _is_sleeping_hits = 0
    return {"hits": 0}


@app.get("/metrics")
async def metrics() -> Response:
    lines = [
        "# HELP vllm:num_requests_running Number of requests currently running.",
        "# TYPE vllm:num_requests_running gauge",
        "vllm:num_requests_running 0",
        "# HELP vllm:prompt_tokens_total Total number of prompt tokens processed.",
        "# TYPE vllm:prompt_tokens_total counter",
        f"vllm:prompt_tokens_total {_prompt_tokens_total}",
        "# HELP vllm:generation_tokens_total Total number of generation tokens processed.",
        "# TYPE vllm:generation_tokens_total counter",
        f"vllm:generation_tokens_total {_completion_tokens_total}",
        "# HELP vllm:time_to_first_token_seconds Time to first token.",
        "# TYPE vllm:time_to_first_token_seconds histogram",
    ]
    # Prometheus text — not a bare str, which FastAPI would serialize as a
    # JSON document (quotes + escaped newlines) that the worker's metrics
    # parser cannot read.
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


def _sse_chunks(model: str, request_id: str) -> list[bytes]:
    """Minimal OpenAI chat-completion SSE: one content delta, then [DONE]."""
    created = int(time.time())
    first = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": _FILLER[:80]}, "finish_reason": None}],
    }
    last = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": _USAGE,
    }
    return [
        f"data: {json.dumps(first, separators=(',', ':'))}\n\n".encode(),
        f"data: {json.dumps(last, separators=(',', ':'))}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]


@app.post("/v1/chat/completions")
async def chat_completions(req: _ChatRequest):
    request_id = _bump_usage()
    model = req.model or MODEL_NAME
    if req.stream:

        async def _gen():
            for chunk in _sse_chunks(model, request_id):
                yield chunk

        return StreamingResponse(_gen(), media_type="text/event-stream")
    return chat_completion_payload(
        model,
        _FILLER,
        request_id=request_id,
        created=int(time.time()),
        prompt_tokens=_USAGE["prompt_tokens"],
        completion_tokens=_USAGE["completion_tokens"],
    )


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LOGOS_BENCH_LANE_PORT", "11436")), log_level="warning")
