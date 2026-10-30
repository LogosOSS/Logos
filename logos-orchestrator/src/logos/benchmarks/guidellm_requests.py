"""Adapt frozen request bodies to GuideLLM's scheduler, HTTP transport and metrics."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from importlib.metadata import version
from pathlib import Path

from guidellm.backends.openai.http import OpenAIHTTPBackend
from guidellm.backends.openai.request_handlers import ChatCompletionsRequestHandler
from guidellm.benchmark.benchmarker import Benchmarker
from guidellm.benchmark.profiles.concurrent import ConcurrentProfile
from guidellm.benchmark.schemas import GenerativeBenchmark, GenerativeBenchmarkAccumulator
from guidellm.scheduler import ConstraintsInitializerFactory, NonDistributedEnvironment
from guidellm.scheduler.schemas.conversation_graph import GenerativeConversationGraph, GenerativeConversationNode
from guidellm.schemas import GenerationRequest, GenerationRequestArguments
from guidellm.schemas.backends import OpenAIHTTPBackendArgs
from guidellm.schemas.benchmark.profiles import ConcurrentProfileArgs
from guidellm.schemas.scheduler import ConstraintArgs


class FrozenRequestHandler(ChatCompletionsRequestHandler):
    """Preserve caller controls and require an accounted, complete response."""

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.finish_reason = None
        self.terminal = False

    def add_streaming_line(self, line):
        if not line.startswith("data:"):
            return 0
        raw = line[5:].strip()
        if raw == "[DONE]":
            self.terminal = True
            return None
        data = json.loads(raw)
        if data.get("error"):
            raise RuntimeError("Frozen request stream reported an inference error")
        if self.source.get("operation", "chat/completions") == "messages":
            return self._messages_event(data)
        for choice in data.get("choices", []):
            if choice.get("index", 0) == 0 and choice.get("finish_reason") is not None:
                self.finish_reason = choice["finish_reason"]
        return super().add_streaming_line(line)

    def _messages_event(self, data):
        """Translate native Messages SSE events into GuideLLM's response parser."""
        event = data.get("type")
        delta = data.get("delta") or {}
        block = data.get("content_block") or {}
        if event == "message_stop":
            self.terminal = True
            return None
        if event == "message_start":
            usage = data.get("message", {}).get("usage") or {}
            self.streaming_usage = {"prompt_tokens": usage.get("input_tokens", 0)}
        if event == "message_delta":
            self.finish_reason = delta.get("stop_reason") or self.finish_reason
            usage = data.get("usage") or {}
            if "output_tokens" in usage:
                self.streaming_usage = {**(self.streaming_usage or {}), "completion_tokens": usage["output_tokens"]}
        translated = {}
        if event in {"content_block_start", "content_block_delta"}:
            content = block if event == "content_block_start" else delta
            if content.get("text"):
                translated["content"] = content["text"]
            if content.get("thinking"):
                translated["reasoning_content"] = content["thinking"]
            if block.get("type") == "tool_use":
                translated["tool_calls"] = [
                    {
                        "index": data.get("index", 0),
                        "id": block["id"],
                        "type": "function",
                        "function": {"name": block["name"], "arguments": ""},
                    }
                ]
            elif delta.get("type") == "input_json_delta":
                translated["tool_calls"] = [
                    {
                        "index": data.get("index", 0),
                        "function": {"arguments": delta.get("partial_json", "")},
                    }
                ]
        if not translated:
            return 0
        return super().add_streaming_line("data: " + json.dumps({"choices": [{"index": 0, "delta": translated}]}))

    def post_validation(self, response):
        tokens = (self.streaming_usage or {}).get("completion_tokens")
        body = self.source["body"]
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        reason = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(
            self.finish_reason, self.finish_reason
        )
        completed_budget = reason == "length" and type(limit) is int and limit > 0 and tokens == limit
        if not self.terminal or (reason not in {"stop", "tool_calls", "function_call"} and not completed_budget):
            raise RuntimeError(
                "Frozen response was incomplete or reached an output/context limit "
                f"(source_log_id={self.source['source_log_id']}, finish_reason={self.finish_reason!r}, "
                f"stream_complete={self.terminal})"
            )
        if type(tokens) is not int or tokens < 0:
            raise RuntimeError("Frozen response omitted valid output token usage")
        response.response_metrics = {
            "source_log_id": self.source["source_log_id"],
            "finish_reason": reason,
            **({"native_finish_reason": self.finish_reason} if self.source.get("operation") == "messages" else {}),
        }


class FrozenRequests:
    """Finite, ordered conversations: one measured request per saved input."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    @property
    def info(self):
        return {key: self._snapshot[key] for key in ("id", "request_count")}

    def __iter__(self):
        for source in self._snapshot["requests"]:
            yield GenerativeConversationGraph.from_nodes_with_parents(
                nodes={
                    "request": GenerativeConversationNode(
                        node_id="request",
                        agent_id="request",
                        request=GenerationRequest(columns={"source": [copy.deepcopy(source)]}),
                    )
                },
                parents_by_node={},
            )


class FrozenHTTPBackend(OpenAIHTTPBackend):
    """Use GuideLLM transport without its synthetic prompt or tool overrides."""

    @property
    def processes_limit(self):
        return 1

    async def _prepare_resolve_request(self, request, history=None):
        if history:
            raise ValueError("Frozen requests already contain their complete conversation history")
        source = request.columns["source"][0]
        operation = source.get("operation", "chat/completions")
        if operation not in {"chat/completions", "messages"}:
            raise ValueError("Unsupported frozen request operation")
        body = {**copy.deepcopy(source["body"]), "model": await self.default_model(), "stream": True}
        if operation == "chat/completions":
            body["stream_options"] = {"include_usage": True}
        arguments = GenerationRequestArguments(body=body, stream=True, headers=self._args.extras.headers)
        base = self._args.target.rstrip("/")
        prefix = base if base.endswith("/v1") else base + "/v1"
        return (
            FrozenRequestHandler(source),
            arguments,
            {
                "url": prefix + "/" + operation,
                "method": "POST",
                "headers": self._build_headers(arguments.headers),
                "json": body,
            },
        )


async def measure_frozen_requests(*, snapshot, target, model, api_key, request_headers, concurrency, seed=42):
    """GuideLLM owns execution, cancellation, timings and statistical aggregation."""
    sources = snapshot["requests"]
    if len(sources) != snapshot["request_count"] or len({source["source_log_id"] for source in sources}) != len(
        sources
    ):
        raise ValueError("Frozen inputs must contain exactly the declared number of distinct requests")
    backend = FrozenHTTPBackend(
        OpenAIHTTPBackendArgs(
            target=target,
            model=model,
            api_key=api_key,
            validate_backend=False,
            verify=True,
            timeout=7200,
            extras={"headers": request_headers or {}},
        )
    )
    constraints = {
        kind: ConstraintsInitializerFactory.create(ConstraintArgs.model_validate({"kind": kind, "count": count}))
        for kind, count in (("max_requests", snapshot["request_count"]), ("max_errors", 1))
    }
    args = ConcurrentProfileArgs(streams=[concurrency])
    profile = ConcurrentProfile(args=args, random_seed=seed, constraints=constraints)
    benchmarks = []
    async for benchmark in Benchmarker().run(
        accumulator_class=GenerativeBenchmarkAccumulator,
        benchmark_class=GenerativeBenchmark,
        requests=FrozenRequests(snapshot),
        backend=backend,
        profile=profile,
        environment=NonDistributedEnvironment(),
        warmup=args.warmup,
        cooldown=args.cooldown,
        sample_size=None,
    ):
        metrics = benchmark.metrics.model_dump(mode="json")
        samples = []
        for stat in benchmark.requests.successful:
            samples.append(
                {
                    **stat.response_metrics,
                    "output_tokens": stat.output_tokens,
                    "ttft_ms": stat.time_to_first_token_ms,
                    "latency_ms": stat.request_latency * 1000,
                    "output_tokens_per_second": stat.output_tokens_per_second,
                }
            )
        order = {source["source_log_id"]: index for index, source in enumerate(snapshot["requests"])}
        if len(samples) != snapshot["request_count"] or {row["source_log_id"] for row in samples} != set(order):
            raise RuntimeError("GuideLLM did not complete every frozen input exactly once")
        samples.sort(key=lambda row: order[row["source_log_id"]])
        metrics["replay_samples"] = samples
        metrics["aggregate_output_tokens_per_second"] = metrics["output_tokens_per_second"]["successful"]["mean"]
        latency = metrics["request_latency"]["successful"]
        metrics["request_latency_ms"] = {
            "successful": {
                "mean": latency["mean"] * 1000,
                "max": latency["max"] * 1000,
                "percentiles": {key: value * 1000 for key, value in latency["percentiles"].items()},
            }
        }
        benchmarks.append(
            {
                "start_time": benchmark.start_time,
                "end_time": benchmark.end_time,
                "metrics": metrics,
                "config": {
                    "profile": {"kind": "concurrent" if concurrency > 1 else "synchronous"},
                    "strategy": {"max_concurrency": concurrency},
                },
            }
        )
    return {"metadata": {"guidellm_version": version("guidellm")}, "benchmarks": benchmarks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text())
    report_path = Path(config.pop("report_path"))
    try:
        result = asyncio.run(measure_frozen_requests(**config))
        report_path.write_text(json.dumps(result, allow_nan=False))
    except Exception as exc:
        # Provider errors can contain echoed prompts or credentials. Only our
        # input-completeness diagnostics are suitable for persisted job failures.
        if str(exc).startswith(("Frozen response", "GuideLLM did not complete")):
            raise SystemExit(str(exc)) from None
        raise SystemExit("GuideLLM frozen-request benchmark failed; inspect the private process diagnostics") from None


if __name__ == "__main__":
    main()
