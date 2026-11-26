"""Validated settings for the next benchmark, independent of stored results."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

QWEN_MTP_CONFIG = '{"method":"mtp","num_speculative_tokens":3}'
QWEN_MTP_MODEL = "Qwen/Qwen3.8-27B"
NGRAM_CONFIG = '{"method":"ngram","num_speculative_tokens":3,"prompt_lookup_min":2,"prompt_lookup_max":5}'
SPECULATIVE_LABELS = {"": "Off", QWEN_MTP_CONFIG: "MTP · 3 draft tokens", NGRAM_CONFIG: "N-gram · 3 draft tokens"}


def canonical_speculative_config(value: str | None) -> str:
    """Recognize only the tuning presets and keep arbitrary worker arguments private."""
    if not value:
        return ""
    try:
        config = json.loads(value)
    except (TypeError, ValueError):
        return "custom"
    if not isinstance(config, dict) or any(type(setting) not in (str, int) for setting in config.values()):
        return "custom"
    if config.get("method") == "qwen3_5_mtp":
        config["method"] = "mtp"
    return next((preset for preset in (QWEN_MTP_CONFIG, NGRAM_CONFIG) if config == json.loads(preset)), "custom")


def canonical_kv_cache_dtype(value: str | None) -> str:
    """Return the effective cache dtype name used for Nightly comparisons and planning."""
    if value == "fp8_e4m3":
        return "fp8"
    return value or "auto"


class ServingOverrides(BaseModel):
    """Allowlisted vLLM settings for benchmark runs."""

    model_config = ConfigDict(extra="forbid")
    tensor_parallel_size: int | None = Field(default=None, ge=1, le=64)
    pipeline_parallel_size: int | None = Field(default=None, ge=1, le=64)
    attention_backend: Literal["FLASHINFER", "FLASH_ATTN", "TRITON_ATTN", "FLEX_ATTENTION", "TURBOQUANT"] | None = None
    kv_cache_dtype: (
        Literal[
            "auto",
            "fp8",
            "fp8_e4m3",
            "fp8_e5m2",
            "turboquant_k8v4",
            "turboquant_4bit_nc",
            "turboquant_k3v4_nc",
            "turboquant_3bit_nc",
        ]
        | None
    ) = None
    kv_cache_memory_bytes: str | None = Field(
        default=None, max_length=32, pattern=r"^(?:[0-9]+(?:[.][0-9]+)?[KMGkmg]?)?$"
    )
    max_num_seqs: int | None = Field(default=None, ge=0, le=65536)
    max_num_batched_tokens: int | None = Field(default=None, gt=0)
    enable_prefix_caching: bool | None = None
    max_model_len: int | None = Field(default=None, ge=0)
    gpu_memory_utilization: float | None = Field(default=None, ge=0.1, le=1)
    quantization: str | None = Field(default=None, max_length=64, pattern=r"^[a-zA-Z0-9_.-]*$")
    dtype: Literal["auto", "float16", "bfloat16", "float32", "half", "float"] | None = None
    enforce_eager: bool | None = None
    disable_custom_all_reduce: bool | None = None
    hf_overrides: dict | None = None
    speculative_config: Literal["", QWEN_MTP_CONFIG, NGRAM_CONFIG] | None = None


class BenchmarkSettings(BaseModel):
    """Public text datasets and reproducible GuideLLM run settings."""

    dataset: str = Field(default="openai/gsm8k", max_length=200, pattern=r"^[\w.-]+/[\w.-]+$")
    subset: str = Field(default="main", min_length=1, max_length=200)
    split: str = Field(default="test", min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    text_column: str = Field(default="question", min_length=1, max_length=200)
    profile: Literal["synchronous", "concurrent"] = "synchronous"
    concurrency: int = Field(default=1, ge=1, le=32)
    seed: int = Field(default=42, ge=0, le=2147483647)
    serving_overrides: ServingOverrides = Field(default_factory=ServingOverrides)
    workload_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class BenchmarkRunSettings(BenchmarkSettings):
    """One complete configuration in a benchmark batch."""

    model_config = ConfigDict(extra="forbid")
    samples: int = Field(default=5, gt=0, le=500)
    max_output_tokens: int = Field(default=512, gt=0, le=4096)

    @model_validator(mode="after")
    def validate_sample_limit(self):
        if not self.workload_id and self.samples > 100:
            raise ValueError("Public datasets support at most 100 samples")
        return self


class BenchmarkBatch(BaseModel):
    """A bounded plan; repetitions do not duplicate configuration payloads."""

    model_config = ConfigDict(extra="forbid")
    configurations: list[BenchmarkRunSettings] = Field(min_length=1, max_length=1000)
    repetitions: int = Field(default=1, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_plan(self) -> "BenchmarkBatch":
        """Reject oversized plans and serving settings that leak between runs."""
        if len(self.configurations) * self.repetitions > 10000:
            raise ValueError("A batch supports at most 10,000 runs.")
        # Every changed serving key must be explicit in every configuration:
        # omissions would otherwise inherit a preceding run's changed value.
        keys = set().union(*(s.serving_overrides.model_dump(exclude_none=True) for s in self.configurations))
        for settings in self.configurations:
            if keys != set(settings.serving_overrides.model_dump(exclude_none=True)):
                raise ValueError("Specify the same serving override keys in every batch configuration.")
        return self
