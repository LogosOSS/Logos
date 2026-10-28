"""Replay a frozen chat workload through the existing signed benchmark route."""

import asyncio
import copy
import json
import math
import time
from datetime import datetime, timezone

import httpx


def distribution(values):
    """Describe observed requests, without inventing missing timing observations."""
    values = sorted(value for value in values if value is not None and math.isfinite(value))
    if not values:
        return {"mean": None, "percentiles": {}}
    return {
        "mean": sum(values) / len(values),
        "percentiles": {
            f"p{p}": values[min(len(values) - 1, math.ceil(len(values) * p / 100) - 1)] for p in (50, 95, 99)
        },
    }


async def measure_request(client, url, body, headers, source_log_id):
    """Use streaming only as a measurement transport; preserve all inference controls."""
    payload = copy.deepcopy(body)
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    started = time.perf_counter()
    first = None
    usage = None
    finish = None
    terminal = False
    async with client.stream("POST", url, json=payload, headers=headers) as response:
        if response.status_code != 200:
            raise RuntimeError(f"Replay request failed with HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            content = line[5:].strip()
            if content == "[DONE]":
                terminal = True
                continue
            data = json.loads(content)
            if data.get("error"):
                raise RuntimeError("Replay stream reported an inference error")
            if data.get("usage") is not None:
                usage = data["usage"]
            for choice in data.get("choices", []):
                if choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta") or {}
                if first is None and any(delta.get(key) for key in ("content", "reasoning_content", "tool_calls")):
                    first = time.perf_counter()
                if choice.get("finish_reason") is not None:
                    finish = choice["finish_reason"]
    ended = time.perf_counter()
    if not terminal or finish not in {"stop", "tool_calls", "function_call"}:
        raise RuntimeError("Replay response was incomplete or reached an output/context limit")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if type(tokens) is not int or tokens < 0:
        raise RuntimeError("Replay response omitted valid output token usage")
    return {
        "source_log_id": source_log_id,
        "output_tokens": tokens,
        "finish_reason": finish,
        "ttft_ms": (first - started) * 1000 if first is not None else None,
        "latency_ms": (ended - started) * 1000,
        "output_tokens_per_second": tokens / max(ended - started, 0.000001),
    }


async def run_replay(*, snapshot, target, model, api_key, request_headers, settings):
    """Run every selected request exactly once; failures cancel and await sibling streams."""
    base = target.rstrip("/")
    url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
    headers = dict(request_headers or {})
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    concurrency = settings.concurrency if settings.profile == "concurrent" else 1
    queue = asyncio.Queue()
    for index, request in enumerate(snapshot["requests"]):
        queue.put_nowait((index, request))
    results = [None] * snapshot["request_count"]
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(7200, connect=30)) as client:

        async def worker():
            while not queue.empty():
                index, request = queue.get_nowait()
                body = {**request["body"], "model": model}
                results[index] = await measure_request(client, url, body, headers, request["source_log_id"])

        async with asyncio.timeout(7200):
            tasks = [asyncio.create_task(worker()) for _ in range(min(concurrency, snapshot["request_count"]))]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    duration = time.perf_counter() - started
    ended_at = datetime.now(timezone.utc)
    total = len(results)
    return {
        "configuration": {
            "tool": "logos-replay",
            "measurement": {"started_at": started_at.isoformat(), "ended_at": ended_at.isoformat()},
            "workload": {key: snapshot[key] for key in ("id", "name", "provenance", "request_count")},
            "benchmark": {"profile": {"kind": settings.profile}, "strategy": {"max_concurrency": concurrency}},
        },
        "dataset": "requests/" + snapshot["id"],
        "sample_size": total,
        "metrics": {
            "request_totals": {"total": total, "successful": total, "errored": 0, "incomplete": 0},
            "aggregate_output_tokens_per_second": sum(row["output_tokens"] for row in results)
            / max(duration, 0.000001),
            "output_tokens_per_second": {
                "successful": distribution([row["output_tokens_per_second"] for row in results])
            },
            "time_to_first_token_ms": {"successful": distribution([row["ttft_ms"] for row in results])},
            "request_latency_ms": {"successful": distribution([row["latency_ms"] for row in results])},
            "replay_samples": results,
        },
        "recorded_at": ended_at,
    }
