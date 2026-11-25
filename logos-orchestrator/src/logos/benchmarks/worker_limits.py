"""Hardware limits for benchmark edits, derived from live worker telemetry."""

import re

from .configuration import QWEN_MTP_CONFIG, QWEN_MTP_MODEL, ServingOverrides
from .guidellm_runner import benchmark_model_lane, lane_serving_configuration

_FP8_MODELS = {
    QWEN_MTP_MODEL,
    "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
    "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
}


def worker_limits(
    snapshot: dict | None,
    model: str,
    *,
    lane: dict | None = None,
    lane_id: str | None = None,
    baseline_config: dict | None = None,
) -> dict:
    runtime = (snapshot or {}).get("runtime") or {}
    summary = runtime.get("devices") or {}
    devices = [(index, d) for index, d in enumerate(summary.get("devices", [])) if d.get("kind") == "nvidia"]
    known = bool(summary.get("nvidia_smi_available"))
    lane = benchmark_model_lane(snapshot, model, lane_id=lane_id) if lane is None else lane
    config = lane.get("lane_config") or {}
    if baseline_config is not None:
        config = {**config, "vllm_config": baseline_config}
        lane = {**lane, "lane_config": config}
    selectors = [runtime.get("gpu_devices", "all")]
    if lane.get("is_static"):
        selectors.append(config.get("gpu_devices", ""))
    for selector in selectors:
        if selector and selector.lower() not in {"all", "none"}:
            allowed = set(selector.split(","))
            devices = [(index, d) for index, d in devices if str((d.get("extra") or {}).get("index", index)) in allowed]
        elif selector == "none":
            devices = []
    memories = [float(d.get("memory_total_mb") or 0) for _, d in devices]
    gpus = []
    restrictions = {}
    for index, device in devices:
        raw_cap = str((device.get("extra") or {}).get("compute_capability") or "")
        capability = raw_cap if re.fullmatch(r"[1-9][0-9]*\.[0-9]+", raw_cap) else None
        gpus.append({"name": device.get("name") or f"GPU {index}", "compute_capability": capability})
        if capability and int(capability.split(".")[0]) < 8:
            for backend in ("FLASHINFER", "FLASH_ATTN"):
                restrictions[backend] = (
                    f"{backend} requires Ampere or newer (SM 8.0+); " f"{gpus[-1]['name']} reports SM {capability}."
                )
    metal = (
        summary.get("mode") == "metal"
        and summary.get("telemetry_available") is True
        and not summary.get("degraded_reason")
        and any(
            d.get("kind") == "metal" and float(d.get("memory_total_mb") or 0) > 0 for d in summary.get("devices", [])
        )
        and runtime.get("gpu_devices", "all") != "none"
    )
    current = lane_serving_configuration(lane)
    night_choices = _nightly_vllm_choices(model, lane, devices, current) if known and not metal else {}
    if "kv_cache_dtype" in night_choices:
        cache = current.get("kv_cache_dtype")
        current["kv_cache_dtype"] = "fp8" if cache == "fp8_e4m3" else cache or "auto"
    if "speculative_config" in night_choices and not current.get("speculative_config"):
        current["speculative_config"] = ""
    return {
        "metal_batching": metal,
        "gpus": gpus,
        "attention_backend_restrictions": restrictions,
        "gpu_count": len(devices) if known else None,
        "gpu_memory_bytes": int(min(memories) * 1024**2) if memories and min(memories) > 0 else None,
        "current": current,
        "nightly_vllm_choices": night_choices,
    }


def _nightly_vllm_choices(model: str, lane: dict, devices: list, current: dict) -> dict[str, tuple[str, ...]]:
    config = lane.get("lane_config") or {}
    pins = str(config.get("gpu_devices") or "").split(",")
    version = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(lane.get("nightly_vllm_version") or ""))
    if not config.get("vllm") or version is None or not all(pin.strip().isdigit() for pin in pins):
        return {}
    # Only the GPU group Nightly actually holds needs to support the variant.
    capabilities = {
        str(extra.get("index")): extra.get("compute_capability")
        for _, device in devices
        if device.get("health") != "faulted" and (extra := device.get("extra"))
    }
    pinned_caps = [str(capabilities.get(pin.strip())) for pin in pins]
    if any(not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", cap) for cap in pinned_caps):
        return {}
    min_sm = min(int(cap.split(".")[0]) for cap in pinned_caps)
    if tuple(map(int, version.groups())) < (0, 26, 0) or lane.get("nightly_blocked_vllm_fields"):
        return {}
    backend = current.get("attention_backend") or ""
    cache = current.get("kv_cache_dtype") or "auto"
    speculative = current.get("speculative_config", "")
    speculative_modes = ("", QWEN_MTP_CONFIG)
    if (
        min_sm < 8
        or backend not in {"", "FLASHINFER", "FLASH_ATTN", "TRITON_ATTN"}
        or cache not in {"auto", "fp8", "fp8_e4m3"}
        or speculative not in speculative_modes
    ):
        return {}
    choices = {}
    if model in _FP8_MODELS and current.get("dtype") != "float32" and (backend != "FLASH_ATTN" or min_sm >= 9):
        choices["kv_cache_dtype"] = ("auto", "fp8")
    if model == QWEN_MTP_MODEL:
        choices["speculative_config"] = speculative_modes
    return choices


def nightly_variation_values(limits: dict) -> dict[str, tuple[str, ...]]:
    """Return supported non-batching parameter values for this worker and lane."""
    return limits.get("nightly_vllm_choices", {})


def validate_worker_overrides(overrides: ServingOverrides, limits: dict, *, nightly: bool = False) -> None:
    requested = overrides.model_dump(exclude_none=True)
    if "speculative_config" in requested and requested["speculative_config"] not in nightly_variation_values(
        limits
    ).get("speculative_config", ()):
        raise ValueError("This worker or lane does not support the requested Nightly speculative decoding mode.")
    if (
        nightly
        and "kv_cache_dtype" in requested
        and requested["kv_cache_dtype"] not in nightly_variation_values(limits).get("kv_cache_dtype", ())
    ):
        raise ValueError("This worker or lane does not support the requested Nightly KV cache dtype.")
    if not requested:
        restriction = limits.get("attention_backend_restrictions", {}).get(limits["current"].get("attention_backend"))
        if restriction:
            raise ValueError(restriction)
        return
    seqs = requested.get("max_num_seqs", limits["current"].get("max_num_seqs")) or 0
    tokens = requested.get("max_num_batched_tokens", limits["current"].get("max_num_batched_tokens")) or 0
    if tokens and seqs and tokens < seqs:
        raise ValueError("Max batched tokens must be at least max sequences.")
    if limits.get("metal_batching"):
        if set(requested) - {"max_num_seqs", "max_num_batched_tokens"}:
            raise ValueError("Metal workers support batching overrides only.")
        return
    count = limits["gpu_count"]
    if count is None:
        raise ValueError(
            "Worker GPU information is unavailable. Wait for its next status report before changing vLLM settings."
        )
    if count == 0:
        raise ValueError("This worker has no available NVIDIA GPUs for this model.")
    effective = {**limits["current"], **requested}
    backend = effective.get("attention_backend")
    restriction = limits.get("attention_backend_restrictions", {}).get(backend)
    if restriction:
        raise ValueError(restriction)
    if effective.get("attention_backend") == "TURBOQUANT" and not str(effective.get("kv_cache_dtype", "")).startswith(
        "turboquant_"
    ):
        raise ValueError("TURBOQUANT requires a turboquant KV cache dtype. Select a compatible KV cache dtype first.")
    if backend and backend != "TURBOQUANT" and str(effective.get("kv_cache_dtype", "")).startswith("turboquant_"):
        raise ValueError(
            "TurboQuant KV cache requires the TURBOQUANT attention backend; choose another KV cache dtype."
        )
    tp = int(effective.get("tensor_parallel_size") or 1)
    pp = int(effective.get("pipeline_parallel_size") or 1)
    if tp * pp > count:
        raise ValueError(
            f"Tensor parallel size ({tp}) × pipeline parallel size ({pp}) requires {tp * pp} GPUs, "
            f"but this worker has only {count} available for this model."
        )
    raw_cache = requested.get("kv_cache_memory_bytes")
    if raw_cache:
        value = raw_cache.upper()
        multiplier = {"K": 1024, "M": 1024**2, "G": 1024**3}.get(value[-1], 1)
        cache_bytes = float(value[:-1] if multiplier != 1 else value) * multiplier
        if cache_bytes <= 0:
            raise ValueError("KV cache memory must be greater than zero, or empty to keep the current setting.")
        ceiling = limits.get("gpu_memory_bytes")
        if ceiling is not None and cache_bytes >= ceiling:
            raise ValueError(
                f"KV cache memory per GPU must be smaller than its total memory ({ceiling / 1024**3:g} GiB); "
                "model weights also need GPU memory."
            )
