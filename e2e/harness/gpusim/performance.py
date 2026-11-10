"""Optional timing and event model below the real worker's vLLM boundary."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from harness.gpusim import state as gpustate

DEFAULT_PROFILE = Path(__file__).with_name("timing_profiles") / "dual_ada_v1.json"


def load_timing_profile(path: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    profile = json.loads(path.read_text())
    if profile.get("schema_version") != 1 or not profile.get("synthetic"):
        raise ValueError("unsupported synthetic timing profile")
    for model, values in profile["models"].items():
        for key in ("cold_start_s", "wake_s", "prefill_tokens_s", "decode_tokens_s"):
            if not math.isfinite(float(values[key])) or float(values[key]) <= 0:
                raise ValueError(f"invalid {key} for {model}")
        if float(values["concurrency_slowdown"]) < 0:
            raise ValueError(f"invalid concurrency slowdown for {model}")
    return profile


def record_event(kind: str, lane: Any, **extra: Any) -> None:
    target = os.environ.get("LOGOS_SIM_EVENTS_FILE")
    if not target:
        return
    state = gpustate.load()
    memory = [
        {"device": d.index, "used_mb": state.used_mb(d.index), "total_mb": d.memory_total_mb} for d in state.devices
    ]
    event = {
        "event": kind,
        "at": time.time(),
        "model": lane.model,
        "pid": os.getpid(),
        "memory": memory,
        "gpu_devices": sorted(lane.awake_by_device),
        **extra,
    }
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.write(json.dumps(event) + "\n")
        output.flush()
        fcntl.flock(output.fileno(), fcntl.LOCK_UN)


class PerformanceTiming:
    """Token service with explicit active-sequence slowdown and bounded engine slots."""

    def __init__(self, model: str) -> None:
        self.values = load_timing_profile(Path(os.environ["LOGOS_SIM_TIMING_PROFILE"]))["models"][model]
        self.speed = float(os.environ.get("LOGOS_SIM_TIME_FACTOR", "1"))
        if not math.isfinite(self.speed) or self.speed <= 0:
            raise ValueError("LOGOS_SIM_TIME_FACTOR must be finite and positive")
        self.running = 0
        self.waiting = 0
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(8)
        self.ttfts: list[float] = []
        self.totals: list[float] = []
        self.ready_recorded = False

    def delay(self, tokens: int, phase: str) -> float:
        with self.lock:
            sequences = max(self.running, 1)
        return (
            tokens
            / self.values[f"{phase}_tokens_s"]
            * (1 + self.values["concurrency_slowdown"] * (sequences - 1))
            / self.speed
        )

    def complete(self, handler: Any, lane: Any, path: str, body: dict[str, Any]) -> None:
        chat = path.endswith("/chat/completions")
        tokens = max(1, int(body.get("max_tokens", 16)))
        prompt = sum(len(str(m.get("content", "")).split()) for m in body.get("messages", [])) or 16
        started = time.monotonic()
        with self.lock:
            self.waiting += 1
        self.slots.acquire()
        with self.lock:
            self.waiting -= 1
            self.running += 1
        try:
            lane.requests_total += 1
            lane.prompt_tokens_total += prompt
            request_id = "cmpl-" + uuid.uuid4().hex[:24]
            created = int(time.time())
            time.sleep(self.delay(prompt, "prefill") + self.delay(1, "decode"))
            ttft = time.monotonic() - started
            if not body.get("stream"):
                time.sleep(self.delay(tokens - 1, "decode"))
                lane.generation_tokens_total += tokens
                handler._send(
                    200, handler._final_payload(chat, request_id, created, lane.model, prompt, ["token"] * tokens)
                )
            else:
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("Connection", "keep-alive")
                handler.end_headers()
                emitted = 0
                while emitted < tokens:
                    count = 1 if emitted == 0 else min(16, tokens - emitted)
                    if emitted:
                        time.sleep(self.delay(count, "decode"))
                    chunk = handler._stream_chunk(
                        chat, request_id, created, lane.model, " ".join(["token"] * count), first=emitted == 0
                    )
                    handler.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                    handler.wfile.flush()
                    emitted += count
                    lane.generation_tokens_total += count
                stop = handler._stream_stop(chat, request_id, created, lane.model)
                stop["usage"] = {"prompt_tokens": prompt, "completion_tokens": tokens, "total_tokens": prompt + tokens}
                handler.wfile.write(b"data: " + json.dumps(stop).encode() + b"\n\ndata: [DONE]\n\n")
                handler.wfile.flush()
            with self.lock:
                self.ttfts.append(ttft)
                self.totals.append(time.monotonic() - started)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.lock:
                self.running -= 1
            self.slots.release()

    def metrics(self, lane: Any) -> str:
        label = f'{{model_name="{lane.model}"}}'
        lines = [
            f"vllm:num_requests_waiting{label} {self.waiting}",
            f"vllm:num_requests_running{label} {self.running}",
            f"vllm:gpu_cache_usage_perc{label} 0.12",
            f"vllm:prompt_tokens_total{label} {lane.prompt_tokens_total}",
            f"vllm:generation_tokens_total{label} {lane.generation_tokens_total}",
        ]
        with self.lock:
            samples = {
                "time_to_first_token_seconds": list(self.ttfts),
                "e2e_request_latency_seconds": list(self.totals),
            }
        for name, values in samples.items():
            metric = f"vllm:{name}"
            for bound in [0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, float("inf")]:
                upper = "+Inf" if math.isinf(bound) else str(bound)
                lines.append(
                    f'{metric}_bucket{{model_name="{lane.model}",le="{upper}"}} ' f"{sum(v <= bound for v in values)}"
                )
            lines.extend([f"{metric}_sum{label} {sum(values)}", f"{metric}_count{label} {len(values)}"])
        return "\n".join(lines) + "\n"
