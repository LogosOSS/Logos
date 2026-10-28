"""Dual-node, dual-Ada GPU topology, calibrated profiles, and frozen engine pins.

Public defaults carry **real non-timing** TP/KV/context from the calibration
snapshot (not a uniform 4 GiB/8192 stand-in). Frozen engine pins
(TP/KV bytes/GMU/max_model_len/…) live in a **separate** fixture so the
reconfigured-VRAM path (e.g. Gemma-4b calib KV ≈30 GiB vs engine pin ≈13 GiB)
is exercised under the native cold-GMU guard. Timings are
``accelerated_functional`` only — never timing-faithful or GPU performance claims.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.gpusim.scenario import GpuScenario

#: GPU profile key in ``harness/gpusim/profiles/``.
BENCHMARK_GPU_PROFILE = "rtx6000ada"

#: Per-card VRAM ceiling for the simulated Ada cards (MB).
BENCHMARK_CARD_VRAM_MB = 49_140.0

#: Cards per worker node in the benchmark topology.
BENCHMARK_GPUS_PER_NODE = 2

#: Distinct worker nodes in the benchmark topology.
BENCHMARK_NODE_COUNT = 2

#: Compose service names for the optional benchmark-topology overlay.
BENCHMARK_NODE_SERVICES = ("node-ada-a", "node-ada-b")

#: Five-LLM catalogue (HF model ids).
BENCHMARK_MODELS_5: tuple[str, ...] = (
    "Qwen/Qwen3.6-35B-A3B",
    "meta-llama/Llama-3.1-8B-Instruct",
    "google/gemma-3-12b-it",
    "microsoft/Phi-4-reasoning",
    "google/gemma-3-4b-it",
)

#: Pinned Hugging Face revisions.
BENCHMARK_MODEL_REVISIONS: dict[str, str] = {
    "Qwen/Qwen3.6-35B-A3B": "995ad96eacd98c81ed38be0c5b274b04031597b0",
    "google/gemma-3-12b-it": "96b6f1eccf38110c56df3a15bffe176da04bfd80",
    "google/gemma-3-4b-it": "093f9f388b31de276ce2de164bdc2081324b9767",
    "meta-llama/Llama-3.1-8B-Instruct": "0e9e39f249a16976918f6564b8830bc894c89659",
    "microsoft/Phi-4-reasoning": "1de18ec97600877ce63dbf60c73b998da99f0195",
}

#: Per-request templates. Qwen thinking forced off so sleep/nosleep answer alike.
BENCHMARK_REQUEST_TEMPLATES: dict[str, dict[str, Any]] = {
    "Qwen/Qwen3.6-35B-A3B": {"chat_template_kwargs": {"enable_thinking": False}},
    "meta-llama/Llama-3.1-8B-Instruct": {},
    "google/gemma-3-12b-it": {},
    "microsoft/Phi-4-reasoning": {},
    "google/gemma-3-4b-it": {},
}

#: Traffic patterns (generic public names).
BENCHMARK_TRAFFIC_PATTERNS: tuple[str, ...] = ("burst", "poisson", "sequential", "mixed")

#: Idle → sleep L1 threshold the orchestrator is configured with on the orchestrator.
BENCHMARK_IDLE_SLEEP_L1_S = 60.0

#: Extra bounded wait for the planner to act after idle (seconds).
BENCHMARK_SLEEP_PLANNER_WAIT_S = 90.0

#: Synthetic results label, distinguishing simulated runs from real GPU runs.
SYNTHETIC_RESULTS_LABEL = "synthetic-e2e-private"

#: Explicit timing label — fast cold/wake delays are functional only.
TIMING_MODE = "accelerated_functional"

#: Documented simulation assumptions (reporter / README; not GPU claims).
SIMULATION_ASSUMPTIONS: dict[str, Any] = {
    "timing_mode": TIMING_MODE,
    "scientific_claims": False,
    "host_ram_total_mb": 524_288,
    "sleep_l1_residual": (
        "per-device residual from public fixture sleeping_residual_mb/TP "
        "(container LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_FILE); do not forge DB rows mid-traffic"
    ),
    "calibration_vs_engine": (
        "model_profiles carry calibration KV/ctx; engines.vllm.model_overrides "
        "carry frozen engine pins (Gemma-4b calib KV≈30GiB vs engine≈13GiB)"
    ),
    "fake_vllm_version_kind": "simulated_protocol",
}

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Public calibrated defaults — real TP/KV/ctx (INBOX snapshot). ``base``/``loaded``
#: already include calibration KV; never add ``kv_budget_mb`` into loaded again.
#: ``cold_load_time_s`` / ``wake_from_sleep_time_s`` are accelerated_functional.
_PUBLIC_DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "Qwen/Qwen3.6-35B-A3B": {
        "base_residency_mb": 80_367.0,
        "loaded_vram_mb": 80_367.0,
        "sleeping_residual_mb": 4_327.0,
        "kv_budget_mb": 4_096.0,
        "tensor_parallel_size": 2,
        "max_context_length": 262_144,
        "calibration_max_model_len": 262_144,
        "cold_load_time_s": 2.0,
        "wake_from_sleep_time_s": 0.3,
        "timing_mode": TIMING_MODE,
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
    },
    "meta-llama/Llama-3.1-8B-Instruct": {
        "base_residency_mb": 40_153.0,
        "loaded_vram_mb": 40_153.0,
        "sleeping_residual_mb": 1_097.0,
        "kv_budget_mb": 23_552.0,
        "tensor_parallel_size": 1,
        "max_context_length": 131_072,
        "calibration_max_model_len": 131_072,
        "cold_load_time_s": 1.5,
        "wake_from_sleep_time_s": 0.25,
        "timing_mode": TIMING_MODE,
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
    },
    "google/gemma-3-12b-it": {
        "base_residency_mb": 41_005.0,
        "loaded_vram_mb": 41_005.0,
        "sleeping_residual_mb": 1_253.0,
        "kv_budget_mb": 15_360.0,
        "tensor_parallel_size": 1,
        "max_context_length": 131_072,
        "calibration_max_model_len": 131_072,
        "cold_load_time_s": 1.5,
        "wake_from_sleep_time_s": 0.25,
        "timing_mode": TIMING_MODE,
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
    },
    "microsoft/Phi-4-reasoning": {
        "base_residency_mb": 40_611.0,
        "loaded_vram_mb": 40_611.0,
        "sleeping_residual_mb": 1_241.0,
        "kv_budget_mb": 11_264.0,
        "tensor_parallel_size": 1,
        "max_context_length": 32_768,
        "calibration_max_model_len": 32_768,
        "cold_load_time_s": 1.5,
        "wake_from_sleep_time_s": 0.25,
        "timing_mode": TIMING_MODE,
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
    },
    "google/gemma-3-4b-it": {
        "base_residency_mb": 41_123.0,
        "loaded_vram_mb": 41_123.0,
        "sleeping_residual_mb": 1_115.0,
        # Calibration profile KV (~30 GiB) — NOT the frozen engine pin (~13 GiB).
        "kv_budget_mb": 30_720.0,
        "tensor_parallel_size": 1,
        "max_context_length": 131_072,
        "calibration_max_model_len": 131_072,
        "cold_load_time_s": 1.2,
        "wake_from_sleep_time_s": 0.2,
        "timing_mode": TIMING_MODE,
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
    },
}

#: Active profiles — overridden by ``load_profile_fixture``.
BENCHMARK_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    model: dict(profile) for model, profile in _PUBLIC_DEFAULT_PROFILES.items()
}

#: Frozen engine pins (separate from calibration profiles). Loaded at import.
BENCHMARK_ENGINE_SPECS: dict[str, dict[str, Any]] = {}

#: Bounded default request count for integrated pattern runs (≥ one per model).
BOUNDED_REQUESTS_PER_PATTERN = 10

#: Documented full-size replay knobs (run via ``python -m harness.workload --full-replay``).
FULL_SIZE_REPLAY = {
    "num_samples": 1319,
    "patterns": list(BENCHMARK_TRAFFIC_PATTERNS),
    "models": list(BENCHMARK_MODELS_5),
    "idle_sleep_l1_s": BENCHMARK_IDLE_SLEEP_L1_S,
    "label": SYNTHETIC_RESULTS_LABEL,
    "scientific_claims": False,
    "request_timeout_s": 300.0,
}


def _normalize_kv_pairs(raw_pairs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw_pairs, list):
        return out
    for item in raw_pairs:
        if not isinstance(item, dict):
            continue
        kv = item.get("kv_cache_mb", item.get("kv_mb"))
        maxlen = item.get("max_model_len")
        if kv is None or maxlen is None:
            continue
        pair: dict[str, Any] = {"kv_cache_mb": float(kv), "max_model_len": int(maxlen)}
        if item.get("parallelity") is not None:
            pair["parallelity"] = item["parallelity"]
        out.append(pair)
    return out


def _profile_from_observed(raw: dict[str, Any]) -> dict[str, Any]:
    """Map an observed fixture entry into a seedable calibrated profile.

    ``base_residency_mb`` already includes KV — ``loaded_vram_mb`` is copied as-is.
    """
    base = float(raw["base_residency_mb"])
    loaded = float(raw.get("loaded_vram_mb", base))
    sleeping = float(raw["sleeping_residual_mb"])
    kv = float(raw.get("kv_budget_mb") or 0.0)
    tp = int(raw.get("tensor_parallel_size") or 1)
    max_ctx = raw.get("max_context_length") or raw.get("calibration_max_model_len") or 8192
    pairs = _normalize_kv_pairs(raw.get("kv_cache_to_max_model_len_pairs"))
    profile: dict[str, Any] = {
        "base_residency_mb": base,
        "loaded_vram_mb": loaded,
        "sleeping_residual_mb": sleeping,
        "kv_budget_mb": kv,
        "tensor_parallel_size": tp,
        "max_context_length": int(max_ctx),
        "cold_load_time_s": float(raw.get("cold_load_time_s") or 1.5),
        "wake_from_sleep_time_s": float(raw.get("wake_from_sleep_time_s") or 0.3),
        "residency_source": "calibrated",
        "engine": "vllm",
        "enable_sleep_mode": True,
        "kv_cache_to_max_model_len_pairs": pairs,
    }
    for key in (
        "host_ram_residual_mb",
        "sleep_l1_transient_host_ram_mb",
        "disk_size_bytes",
        "kv_per_token_bytes",
        "num_key_value_heads",
        "max_kv_cache_mb",
        "min_kv_cache_mb",
        "calibration_max_model_len",
        "calibration_max_num_seqs",
    ):
        if raw.get(key) is not None:
            profile[key] = raw[key]
    return profile


def resolve_profile_fixture_path(explicit: str | Path | None = None) -> Path | None:
    """Resolve optional external fixture path (CLI / ``E2E_PROFILE_FIXTURE`` only)."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.environ.get("E2E_PROFILE_FIXTURE", "").strip()
    if env:
        path = Path(env).expanduser()
        return path if path.is_file() else None
    return None


def resolve_engine_specs_path(explicit: str | Path | None = None) -> Path | None:
    """Resolve frozen engine-spec JSON (env / CLI / in-tree public default)."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    env = os.environ.get("E2E_ENGINE_SPECS_FIXTURE", "").strip()
    if env:
        path = Path(env).expanduser()
        return path if path.is_file() else None
    public = _FIXTURES_DIR / "benchmark_engine_specs_public.json"
    return public if public.is_file() else None


def load_engine_specs(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load frozen engine pins into ``BENCHMARK_ENGINE_SPECS``.

    Shape: ``{"models": {model: {"spec": {...}, "gpu_memory_utilization": ...}}}``
    or a flat ``{model: {...}}`` map. Does **not** mutate calibration profiles.
    """
    resolved = resolve_engine_specs_path(path)
    if resolved is None:
        return dict(BENCHMARK_ENGINE_SPECS)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    models = payload.get("models") if isinstance(payload.get("models"), dict) else payload
    loaded: dict[str, dict[str, Any]] = {}
    for model in BENCHMARK_MODELS_5:
        if model not in models:
            raise ValueError(f"engine specs {resolved} missing model {model}")
        entry = models[model]
        if not isinstance(entry, dict):
            raise ValueError(f"engine specs entry for {model} must be an object")
        loaded[model] = dict(entry)
    BENCHMARK_ENGINE_SPECS.clear()
    BENCHMARK_ENGINE_SPECS.update(loaded)
    return dict(BENCHMARK_ENGINE_SPECS)


def sleep_l1_residual_mb_by_model(
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, float]:
    """Per-device sleep_L1 residual MB from calibrated profiles (÷ TP).

    Wired into the nodesim container as ``LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_*`` so
    the fake EngineCore ledger matches public fixture residuals rather than the
    synthetic 3% fallback alone.
    """
    src = profiles if profiles is not None else BENCHMARK_MODEL_PROFILES
    out: dict[str, float] = {}
    for model, profile in src.items():
        total = float(profile.get("sleeping_residual_mb") or 0.0)
        tp = max(int(profile.get("tensor_parallel_size") or 1), 1)
        if total > 0:
            out[model] = total / tp
    return out


def container_simulation_assumptions() -> dict[str, Any]:
    """Assumptions reflecting what the benchmark stack configures in-container."""
    residuals = sleep_l1_residual_mb_by_model()
    return {
        **SIMULATION_ASSUMPTIONS,
        "timing_mode": TIMING_MODE,
        "sleep_l1_residual_mode": "per_model_mb" if residuals else "fraction_of_awake",
        "sleep_l1_residual_mb_by_model": residuals,
        "fake_vllm_version": "0.32.0",
        "fake_vllm_version_kind": "simulated_protocol",
        "fake_vllm_version_note": (
            "Reported version is the emulated EngineCore protocol target, " "not an installed vLLM package."
        ),
    }


def engine_vllm_overrides(model: str) -> dict[str, Any]:
    """Flatten one model's frozen engine pin into ``engines.vllm.model_overrides``."""
    entry = BENCHMARK_ENGINE_SPECS.get(model) or {}
    spec = dict(entry.get("spec") or {})
    out: dict[str, Any] = {}
    for key in (
        "tensor_parallel_size",
        "kv_cache_memory_bytes",
        "max_model_len",
        "max_num_seqs",
        "max_num_batched_tokens",
        "enforce_eager",
        "dtype",
        "block_size",
        "enable_prefix_caching",
        "enable_chunked_prefill",
        "reasoning_parser",
    ):
        if key in spec and spec[key] is not None:
            out[key] = spec[key]
    # Bytes may arrive as int — worker accepts str or int-like.
    if "kv_cache_memory_bytes" in out and not isinstance(out["kv_cache_memory_bytes"], str):
        out["kv_cache_memory_bytes"] = str(int(out["kv_cache_memory_bytes"]))
    gmu = entry.get("gpu_memory_utilization")
    if gmu is not None and gmu != "":
        out["gpu_memory_utilization"] = float(gmu)
    tmpl = spec.get("default_chat_template_kwargs")
    if isinstance(tmpl, dict) and tmpl:
        out["chat_template_kwargs"] = dict(tmpl)
    return out


def load_profile_fixture(path: str | Path | None = None, *, node: str | None = None) -> dict[str, dict[str, Any]]:
    """Load per-model profiles from an observed fixture JSON.

    Fixture shape: ``{"nodes": {"gpu-node-a": {model: {...}, ...}, ...}}``.
    When *node* is omitted, the first node that covers all five models is used.
    Updates module-global ``BENCHMARK_MODEL_PROFILES``.
    """
    resolved = resolve_profile_fixture_path(path)
    if resolved is None:
        return dict(BENCHMARK_MODEL_PROFILES)

    payload = json.loads(resolved.read_text(encoding="utf-8"))
    nodes = payload.get("nodes") or {}
    chosen: dict[str, Any] | None = None
    if node and node in nodes:
        chosen = nodes[node]
    else:
        for _name, models in nodes.items():
            if all(m in models for m in BENCHMARK_MODELS_5):
                chosen = models
                break
        if chosen is None and nodes:
            chosen = next(iter(nodes.values()))
    if not chosen:
        raise ValueError(f"profile fixture {resolved} has no node model maps")

    loaded: dict[str, dict[str, Any]] = {}
    for model in BENCHMARK_MODELS_5:
        if model not in chosen:
            raise ValueError(f"fixture {resolved} missing model {model}")
        loaded[model] = _profile_from_observed(chosen[model])

    BENCHMARK_MODEL_PROFILES.clear()
    BENCHMARK_MODEL_PROFILES.update(loaded)
    return dict(BENCHMARK_MODEL_PROFILES)


@dataclass(frozen=True)
class BenchmarkNodeSpec:
    service: str
    profile: str = BENCHMARK_GPU_PROFILE
    gpu_count: int = BENCHMARK_GPUS_PER_NODE


def benchmark_fleet() -> tuple[BenchmarkNodeSpec, ...]:
    return tuple(BenchmarkNodeSpec(service=name) for name in BENCHMARK_NODE_SERVICES)


def dual_ada_node() -> GpuScenario:
    """One worker: two simulated RTX 6000 Ada cards (benchmark node shape)."""
    return GpuScenario.dual_rtx6000ada()


# Load in-tree public engine pins at import (CI has no private path).
load_engine_specs()
