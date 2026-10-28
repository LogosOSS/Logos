"""Fake vLLM OpenAI server.

Implements the surface ``VllmProcessHandle`` actually talks to — ``/health``,
``/version``, ``/v1/models``, ``/v1/completions``, ``/v1/chat/completions``,
``/metrics``, ``/sleep``, ``/wake_up``, ``/is_sleeping``, ``/reset_mm_cache`` —
and nothing else.

Deliberately stdlib-only: this runs as a subprocess spawned by the worker under
whatever ``python3`` is first on PATH, which is not necessarily the interpreter
the test suite installed its dependencies into.

VRAM is real here in the only sense that matters to the code under test: the
server debits its allocation from the shared simulator state on startup and
credits it back on a clean shutdown, so ``nvidia-smi --query-compute-apps``
reports it exactly the way ``_verify_vram_released`` expects.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from harness.fake_vllm.protocol import chat_completion_payload, models_payload
from harness.gpusim import state as gpustate
from harness.gpusim.scenario import VllmScript

#: Simulated protocol version reported at ``/version`` and ``vllm --version``.
#: This is **not** an installed vLLM wheel — it labels the emulated EngineCore
#: contract the benchmark images target (0.32.x). Override with
#: ``LOGOS_SIM_FAKE_VLLM_VERSION`` when a suite needs a different target.
_DEFAULT_FAKE_VLLM_VERSION = "0.32.0"


def _fake_vllm_version() -> str:
    raw = os.environ.get("LOGOS_SIM_FAKE_VLLM_VERSION", "").strip()
    return raw or _DEFAULT_FAKE_VLLM_VERSION


#: Default simulated protocol version. Prefer :func:`_fake_vllm_version` at
#: call time so ``LOGOS_SIM_FAKE_VLLM_VERSION`` overrides apply; this alias
#: keeps ``from harness.gpusim.server import FAKE_VLLM_VERSION`` stable.
FAKE_VLLM_VERSION = _DEFAULT_FAKE_VLLM_VERSION

#: vLLM's own default when ``--gpu-memory-utilization`` is omitted. The fake
#: EngineCore still gates startup on this reservation even when
#: ``--kv-cache-memory-bytes`` pins the steady-state KV pool (real
#: ``request_memory`` behaviour).
VLLM_DEFAULT_GMU = 0.9

#: Canned completion text. Short and deterministic so streaming tests can
#: assert on exact chunk counts.
COMPLETION_TEXT = "Simulated response from the Logos E2E vLLM stub."

#: Default fraction of the awake per-device claim kept resident after
#: ``/sleep?level=1``. Synthetic approximation (~1 GiB of a ~40 GiB TP1 lane)
#: unless a per-model absolute residual MB map overrides it. Without shrinking
#: the ledger, the worker EMA-corrupts calibrated ``sleeping_residual_mb``
#: toward the full awake footprint and the planner concludes sleep_L1 frees
#: almost nothing.
_DEFAULT_SLEEP_L1_RESIDUAL_FRAC = 0.03


def _sleep_l1_residual_frac() -> float:
    raw = os.environ.get("LOGOS_SIM_SLEEP_L1_RESIDUAL_FRAC", "").strip()
    if not raw:
        return _DEFAULT_SLEEP_L1_RESIDUAL_FRAC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_SLEEP_L1_RESIDUAL_FRAC
    return value if 0.0 < value < 1.0 else _DEFAULT_SLEEP_L1_RESIDUAL_FRAC


def _sleep_l1_residual_mb_by_model() -> dict[str, float]:
    """Optional absolute residual MB (per device) keyed by model id.

    Sources, first match wins:

    * ``LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON`` — inline ``{"model": mb, ...}``
    * ``LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_FILE`` — path to the same JSON object

    Public fixtures may ship calibrated residuals without requiring private
    files; absent entries fall back to the fractional approximation.
    """
    raw = os.environ.get("LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON", "").strip()
    if not raw:
        path = os.environ.get("LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_FILE", "").strip()
        if path:
            try:
                raw = Path(path).read_text(encoding="utf-8")
            except OSError:
                return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in parsed.items():
        try:
            out[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def sleep_residual_mb_for(model: str, awake_mb: float) -> float:
    """Resident MB kept on one device after sleep_L1 for *model*."""
    absolute = _sleep_l1_residual_mb_by_model().get(model)
    if absolute is not None and absolute > 0:
        return max(float(absolute), 1.0)
    return max(awake_mb * _sleep_l1_residual_frac(), 1.0)


def simulation_assumptions() -> dict[str, Any]:
    """Explicit sim assumptions for report emitters (fractional residual, etc.)."""
    frac = _sleep_l1_residual_frac()
    by_model = _sleep_l1_residual_mb_by_model()
    return {
        "fake_vllm_version": _fake_vllm_version(),
        "fake_vllm_version_kind": "simulated_protocol",
        "fake_vllm_version_note": (
            "Reported version is the emulated EngineCore protocol target, " "not an installed vLLM package."
        ),
        "sleep_l1_residual_frac": frac,
        "sleep_l1_residual_mb_by_model": dict(by_model),
        "sleep_l1_residual_mode": "per_model_mb" if by_model else "fraction_of_awake",
        "sleep_l1_residual_note": (
            f"Default sleep_L1 residual is {frac:.3%} of the awake per-device "
            "claim (synthetic approximation) unless overridden per model via "
            "LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON / _FILE."
        ),
    }


#: Default ``max_model_len`` advertised on ``/v1/models`` when the CLI omits
#: ``--max-model-len`` (generic unit tests). Benchmark lanes pass the real pin.
_DEFAULT_MAX_MODEL_LEN = 32768


class Lane:
    """Mutable serving state for the one model this process hosts."""

    def __init__(self, model: str, script: VllmScript, vram_mb: float) -> None:
        self.model = model
        self.script = script
        self.vram_mb = vram_mb
        self.started_at = time.time()
        self.sleeping = False
        self.requests_total = 0
        self.prompt_tokens_total = 0
        self.generation_tokens_total = 0
        #: Awake per-device claims (MB); restored on ``/wake_up``.
        self.awake_by_device: dict[int, float] = {}
        self.pid: int = 0
        #: Startup GMU used for the free≥gmu×total gate (may differ from claim).
        self.startup_gmu: float = VLLM_DEFAULT_GMU
        #: Served context window from ``--max-model-len`` (worker calibration).
        self.max_model_len: int = _DEFAULT_MAX_MODEL_LEN

    @property
    def ready(self) -> bool:
        return time.time() - self.started_at >= self.script.ready_after_s


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _claim_mb(sim: gpustate.GpuSimState, pid: int, device_index: int) -> float:
    for alloc in sim.allocations:
        if alloc.pid == pid and alloc.device_index == device_index and not alloc.leaked:
            return float(alloc.mb)
    return 0.0


def assert_startup_gmu_free(sim: gpustate.GpuSimState, device_index: int, gmu: float) -> None:
    """Real vLLM ``request_memory`` gate: free ≥ gmu × physical total per GPU.

    Runs even when the lane pins ``--kv-cache-memory-bytes``; that pin sizes the
    steady-state KV pool and is independent of this startup reservation check.
    """
    device = sim.device(device_index)
    if device is None:
        raise gpustate.GpuSimStateError(f"no simulated device with index {device_index}")
    total = device.memory_total_mb
    need = float(gmu) * total
    free = sim.free_mb(device_index)
    if free + 1e-6 < need:
        raise gpustate.GpuSimStateError(
            f"Free memory on device cuda:{device_index} ({free:.2f} MB) is less than "
            f"desired GPU memory utilization ({gmu:.4f} × {total:.2f} MB = {need:.2f} MB). "
            "Decrease gpu_memory_utilization or reduce GPU memory used by other processes."
        )


def assert_wake_capacity(sim: gpustate.GpuSimState, pid: int, awake_by_device: dict[int, float]) -> None:
    """Refuse wake if restoring awake claims would exceed any GPU's physical total."""
    for index, awake_mb in awake_by_device.items():
        device = sim.device(index)
        if device is None:
            raise gpustate.GpuSimStateError(f"no simulated device with index {index}")
        current = _claim_mb(sim, pid, index)
        others = sim.used_mb(index) - current
        if others + float(awake_mb) > device.memory_total_mb + 1e-6:
            raise gpustate.GpuSimStateError(
                f"wake restore of {awake_mb:.2f} MB on GPU {index} exceeds physical "
                f"capacity (others={others:.2f} MB, total={device.memory_total_mb:.2f} MB)"
            )


class Handler(BaseHTTPRequestHandler):
    lane: Lane  # injected on the server instance, see run()

    protocol_version = "HTTP/1.1"

    # Silence per-request logging; the worker streams our stdout into its own
    # log and access lines would drown the startup banner tests assert on.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    # -- helpers ----------------------------------------------------------

    def _send(self, status: int, payload: Any = None, *, content_type: str = "application/json") -> None:
        body = b"" if payload is None else _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        lane = self.server.lane  # type: ignore[attr-defined]

        if path == "/health":
            self._send(200 if lane.ready else 503)
        elif path == "/version":
            version = _fake_vllm_version()
            self._send(
                200,
                {
                    "version": version,
                    "simulated": True,
                    "version_kind": "simulated_protocol",
                    "note": "simulated EngineCore protocol target, not an installed vLLM wheel",
                },
            )
        elif path == "/v1/models":
            self._send(200, self._models_payload(lane))
        elif path == "/metrics":
            self._send_text(200, self._metrics_text(lane))
        elif path == "/is_sleeping":
            if lane.script.wedge_engine_core:
                # The API server is alive but its EngineCore RPC never answers.
                # Block forever: the worker's own timeout is what must fire.
                while True:
                    time.sleep(3600)
            self._send(200, {"is_sleeping": lane.sleeping})
        else:
            self._send(404, {"error": {"message": f"Unknown route {path}", "type": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        lane = self.server.lane  # type: ignore[attr-defined]

        if path == "/sleep":
            self._handle_sleep(lane, query)
        elif path == "/wake_up":
            self._handle_wake(lane)
        elif path == "/reset_mm_cache":
            self._send(200, {})
        elif path in ("/v1/chat/completions", "/v1/completions"):
            self._completion(lane, path, self._read_json())
        else:
            self._send(404, {"error": {"message": f"Unknown route {path}", "type": "not_found"}})

    def _handle_sleep(self, lane: Lane, query: dict[str, list[str]]) -> None:
        if lane.script.refuse_sleep:
            self._send(500, {"error": "Sleep mode is not enabled for this engine"})
            return
        level = int((query.get("level") or ["1"])[0])
        if level >= 1 and lane.awake_by_device and not lane.sleeping:
            try:
                with gpustate.mutate() as sim:
                    # Validate every TP device can be resized, then apply under
                    # the same lock so a mid-TP failure never persists a partial.
                    targets = {
                        index: sleep_residual_mb_for(lane.model, awake_mb)
                        for index, awake_mb in lane.awake_by_device.items()
                    }
                    for index in targets:
                        if sim.device(index) is None:
                            raise gpustate.GpuSimStateError(f"no simulated device with index {index}")
                    for index, residual_mb in targets.items():
                        sim.resize(lane.pid, index, residual_mb)
            except gpustate.GpuSimStateError as exc:
                # Preserve prior sleeping flag + ledger; never fake success.
                self._send(500, {"error": {"message": str(exc), "type": "gpusim_state_error"}})
                return
        lane.sleeping = True
        self._send(200, {"is_sleeping": True, "level": level})

    def _handle_wake(self, lane: Lane) -> None:
        if lane.script.slow_wake_s:
            time.sleep(lane.script.slow_wake_s)
        if lane.sleeping and lane.awake_by_device:
            try:
                with gpustate.mutate() as sim:
                    assert_wake_capacity(sim, lane.pid, lane.awake_by_device)
                    for index, awake_mb in lane.awake_by_device.items():
                        sim.resize(lane.pid, index, awake_mb)
            except gpustate.GpuSimStateError as exc:
                # Stay asleep with residual claims intact — do not report awake
                # under OOM, and do not commit a partial TP restore.
                message = str(exc)
                oom = "exceeds physical" in message or "wake restore" in message
                self._send(
                    507 if oom else 500,
                    {"error": {"message": message, "type": "out_of_memory" if oom else "gpusim_state_error"}},
                )
                return
        lane.sleeping = False
        self._send(200, {"is_sleeping": False})

    # -- payloads ---------------------------------------------------------

    @staticmethod
    def _models_payload(lane: Lane) -> dict[str, Any]:
        return models_payload(
            lane.model,
            owned_by="vllm",
            created=int(lane.started_at),
            max_model_len=int(lane.max_model_len),
        )

    def _completion(self, lane: Lane, path: str, body: dict[str, Any]) -> None:
        if lane.sleeping:
            self._send(503, {"error": {"message": "engine is sleeping", "type": "service_unavailable"}})
            return

        lane.requests_total += 1
        prompt_tokens = 16
        words = COMPLETION_TEXT.split()
        lane.prompt_tokens_total += prompt_tokens
        lane.generation_tokens_total += len(words)

        chat = path.endswith("/chat/completions")
        request_id = f"cmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        model = body.get("model") or lane.model

        if not body.get("stream"):
            self._send(200, self._final_payload(chat, request_id, created, model, prompt_tokens, words))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            for index, word in enumerate(words):
                chunk = self._stream_chunk(chat, request_id, created, model, word, first=index == 0)
                self.wfile.write(b"data: " + _json_bytes(chunk) + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: " + _json_bytes(self._stream_stop(chat, request_id, created, model)) + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The client hung up mid-stream. That is a scenario under test, not
            # an error here — let the connection die quietly.
            return

    @staticmethod
    def _final_payload(
        chat: bool, request_id: str, created: int, model: str, prompt_tokens: int, words: list[str]
    ) -> dict[str, Any]:
        if chat:
            return chat_completion_payload(
                model,
                " ".join(words),
                request_id=request_id,
                created=created,
                prompt_tokens=prompt_tokens,
                completion_tokens=len(words),
                prompt_tokens_details={"cached_tokens": 0},
            )
        text = " ".join(words)
        choice = {"index": 0, "text": text, "finish_reason": "stop"}
        return {
            "id": request_id,
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [choice],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": len(words),
                "total_tokens": prompt_tokens + len(words),
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }

    @staticmethod
    def _stream_chunk(chat: bool, request_id: str, created: int, model: str, word: str, first: bool) -> dict[str, Any]:
        piece = word if first else f" {word}"
        delta: dict[str, Any] = {"content": piece}
        if first and chat:
            delta["role"] = "assistant"
        choice = (
            {"index": 0, "delta": delta, "finish_reason": None}
            if chat
            else {"index": 0, "text": piece, "finish_reason": None}
        )
        return {
            "id": request_id,
            "object": "chat.completion.chunk" if chat else "text_completion",
            "created": created,
            "model": model,
            "choices": [choice],
        }

    @staticmethod
    def _stream_stop(chat: bool, request_id: str, created: int, model: str) -> dict[str, Any]:
        choice = (
            {"index": 0, "delta": {}, "finish_reason": "stop"}
            if chat
            else {"index": 0, "text": "", "finish_reason": "stop"}
        )
        return {
            "id": request_id,
            "object": "chat.completion.chunk" if chat else "text_completion",
            "created": created,
            "model": model,
            "choices": [choice],
        }

    @staticmethod
    def _metrics_text(lane: Lane) -> str:
        """Prometheus exposition using vLLM's real metric names.

        Only the series ``get_backend_metrics`` parses are emitted; anything
        else would be dead weight the worker skips anyway.
        """
        labels = f'{{model_name="{lane.model}"}}'
        ttft_count = max(lane.requests_total, 0)
        lines = [
            "# TYPE vllm:num_requests_waiting gauge",
            f"vllm:num_requests_waiting{labels} 0.0",
            "# TYPE vllm:num_requests_running gauge",
            f"vllm:num_requests_running{labels} 0.0",
            "# TYPE vllm:gpu_cache_usage_perc gauge",
            f"vllm:gpu_cache_usage_perc{labels} 0.12",
            "# TYPE vllm:gpu_prefix_cache_queries_total counter",
            f"vllm:gpu_prefix_cache_queries_total{labels} {float(ttft_count * 16):.1f}",
            "# TYPE vllm:gpu_prefix_cache_hits_total counter",
            f"vllm:gpu_prefix_cache_hits_total{labels} {float(ttft_count * 4):.1f}",
            "# TYPE vllm:prompt_tokens_total counter",
            f"vllm:prompt_tokens_total{labels} {float(lane.prompt_tokens_total):.1f}",
            "# TYPE vllm:generation_tokens_total counter",
            f"vllm:generation_tokens_total{labels} {float(lane.generation_tokens_total):.1f}",
            "# TYPE vllm:time_to_first_token_seconds histogram",
            f'vllm:time_to_first_token_seconds_bucket{{model_name="{lane.model}",le="0.1"}} {float(ttft_count):.1f}',
            f'vllm:time_to_first_token_seconds_bucket{{model_name="{lane.model}",le="+Inf"}} {float(ttft_count):.1f}',
            f"vllm:time_to_first_token_seconds_sum{labels} {ttft_count * 0.08:.4f}",
            f"vllm:time_to_first_token_seconds_count{labels} {float(ttft_count):.1f}",
            "# TYPE vllm:time_per_output_token_seconds histogram",
            f'vllm:time_per_output_token_seconds_bucket{{model_name="{lane.model}",le="+Inf"}} {float(ttft_count):.1f}',
            f"vllm:time_per_output_token_seconds_sum{labels} {ttft_count * 0.01:.4f}",
            f"vllm:time_per_output_token_seconds_count{labels} {float(ttft_count):.1f}",
            "# TYPE vllm:e2e_request_latency_seconds histogram",
            f'vllm:e2e_request_latency_seconds_bucket{{model_name="{lane.model}",le="+Inf"}} {float(ttft_count):.1f}',
        ]
        return "\n".join(lines) + "\n"


def _startup_banner(lane: Lane, port: int) -> str:
    """Lines the worker parses out of the startup log.

    ``Maximum concurrency …`` feeds ``VllmProcessHandle.max_concurrency`` and
    calibration's served-context extraction, so its shape has to match
    ``_VLLM_MAX_CONCURRENCY_RE`` exactly.
    """
    version = _fake_vllm_version()
    lines = [
        f"INFO [api_server.py:1000] vLLM API server version {version} (simulated protocol)",
        f"INFO [core.py:200] Loading model {lane.model}...",
        f"INFO [kv_cache_utils.py:900] Maximum concurrency for "
        f"{lane.script.max_concurrency_tokens:,} tokens per request: {lane.script.max_concurrency:.2f}x",
        f"INFO [api_server.py:1600] Starting vLLM API server on http://0.0.0.0:{port}",
    ]
    if lane.script.emit_dev_mode_warning:
        lines.insert(0, "WARNING [api_server.py:100] SECURITY WARNING: Development endpoints are enabled")
    return "\n".join(lines)


def run(
    model: str,
    port: int,
    script: VllmScript,
    vram_by_device: dict[int, float],
    *,
    startup_gmu: float | None = None,
    max_model_len: int | None = None,
) -> int:
    """Serve until SIGTERM, holding each device's own allocation in the simulator.

    Keyed per device because a mixed-architecture tensor-parallel lane claims a
    different amount on each card; one figure for the whole lane would book the
    first GPU's share against all of them.

    ``startup_gmu`` is the free≥gmu×total gate (real ``request_memory``). It is
    independent of ``vram_by_device``, which is the synthetic/script ledger claim
    (model+KV footprint). When omitted, the vLLM default 0.9 is used — matching
    admission when the CLI flag is absent.

    ``max_model_len`` is advertised on ``/v1/models`` (worker reads it for
    calibration). Defaults to 32768 when the CLI omits ``--max-model-len``.
    """
    lane = Lane(model=model, script=script, vram_mb=sum(vram_by_device.values()))
    pid = os.getpid()
    lane.pid = pid
    gmu = float(startup_gmu) if startup_gmu is not None else VLLM_DEFAULT_GMU
    lane.startup_gmu = gmu
    if max_model_len is not None and int(max_model_len) > 0:
        lane.max_model_len = int(max_model_len)

    # Bind before claiming VRAM. Binding is the step that can still fail (a port
    # collision — `lane.free_port()` hands out a port it has already closed), and
    # an allocation recorded before it would never be released: the simulator
    # would carry a phantom allocation for a process that never ran, and every
    # later capacity assertion in that session would be measured against it.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.lane = lane  # type: ignore[attr-defined]
    server.daemon_threads = True

    claimed = {index: mb for index, mb in vram_by_device.items() if mb > 0}
    lane.awake_by_device = dict(claimed)

    if script.oom_on_start:
        print(
            f"ERROR [gpu_worker.py:0] ValueError: Free memory on device cuda:0 "
            f"is less than desired GPU memory utilization (forced oom_on_start; "
            f"gmu={gmu})",
            flush=True,
        )
        server.server_close()
        return 1

    if claimed:
        try:
            with gpustate.mutate() as sim:
                # Gate first on every TP device, then allocate. Generic
                # ``allocate`` stays unbounded for fault-injection tests; the
                # strict contract lives at this fake-server boundary.
                for index in claimed:
                    assert_startup_gmu_free(sim, index, gmu)
                for index, mb in claimed.items():
                    sim.allocate(pid, index, mb)
        except gpustate.GpuSimStateError as exc:
            print(f"ERROR [gpu_worker.py:0] ValueError: {exc}", flush=True)
            server.server_close()
            return 1

    print(_startup_banner(lane, port), flush=True)

    stopping = threading.Event()

    def _release() -> None:
        if not claimed:
            return
        try:
            with gpustate.mutate() as sim:
                if script.leak_vram:
                    # The process is going away but the driver never reclaims
                    # the context — the stuck-VRAM signature.
                    sim.mark_leaked(pid)
                else:
                    sim.release(pid)
        except gpustate.GpuSimStateError:
            pass

    def _on_signal(signum: int, _frame: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        print(f"INFO [api_server.py:1700] Received signal {signum}, shutting down", flush=True)
        _release()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        if not stopping.is_set():
            _release()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via bin/vllm
    sys.exit(
        run(
            model=sys.argv[1],
            port=int(sys.argv[2]),
            script=VllmScript.read(),
            vram_by_device={0: float(sys.argv[3])},
        )
    )
