"""Calibration and serving share prefix settings and reject incompatible profiles."""

from __future__ import annotations

import pytest

from logos_worker_node.calibration import _build_vllm_cmd
from logos_worker_node.model_profiles import ModelProfileRegistry
from logos_worker_node.models import AppConfig, DeviceInfo, LaneConfig, VllmConfig, WorkerConfig
from logos_worker_node.profile_fingerprint import compute_calibration_key, key_hash
from logos_worker_node.vllm_process import VllmProcessHandle

_PREFIX_FLAGS = {"--enable-prefix-caching", "--no-enable-prefix-caching"}


@pytest.fixture(autouse=True)
def _cuda(monkeypatch):
    monkeypatch.setenv("LOGOS_WORKER_BACKEND", "cuda")
    monkeypatch.setattr("logos_worker_node.calibration.probe_attention_backend", lambda plan: None)


def _key(plan: dict) -> dict:
    gpu = DeviceInfo(
        device_id="gpu0",
        name="NVIDIA RTX A4000",
        memory_total_mb=16376.0,
        extra={"index": 0, "compute_capability": "8.6"},
    )
    return compute_calibration_key(AppConfig(), plan, [gpu], "0.32.0")


@pytest.mark.parametrize(
    "enabled,flag",
    [
        (None, "--enable-prefix-caching"),
        (True, "--enable-prefix-caching"),
        (False, "--no-enable-prefix-caching"),
    ],
)
def test_calibration_and_serving_pass_the_same_prefix_caching_flag(enabled, flag):
    plan = {"model": "org/m", "tensor_parallel_size": 1, "max_model_len": 1000}
    if enabled is not None:
        plan["enable_prefix_caching"] = enabled
    calibration = _build_vllm_cmd(plan, "vllm", "127.0.0.1", 11499, "1G")
    handle = VllmProcessHandle("lane-test", 19000, WorkerConfig())
    handle._resolve_vllm_binary = lambda _configured: ["/tmp/vllm"]
    config = VllmConfig() if enabled is None else VllmConfig(enable_prefix_caching=enabled)
    serving = handle._build_cmd(LaneConfig(model="org/m", vllm=True, vllm_config=config))
    assert [arg for arg in calibration if arg in _PREFIX_FLAGS] == [flag]
    assert [arg for arg in serving if arg in _PREFIX_FLAGS] == [flag]


@pytest.mark.parametrize(
    "backend,plan,legacy_hash",
    [
        ("cuda", {"model": "org/m"}, "e7d46be7f0addf088044fa46849e326beffd8f1bb3d9655d8bff4d2c1ce4e2cc"),
        (
            "cuda",
            {"model": "org/m", "enable_prefix_caching": True},
            "9509657d26dd9f0f68bf3f2855ce9f0487febf96f2ec69df1f0360ec8e73b044",
        ),
        (
            "metal",
            {"model": "org/m", "enable_prefix_caching": False},
            "99f0b74b253786aa6e71d9ba3af265fe804512a514eb5042468757bc915faf99",
        ),
    ],
)
def test_unaffected_profiles_keep_their_stored_key_hash(backend, plan, legacy_hash, monkeypatch):
    monkeypatch.setenv("LOGOS_WORKER_BACKEND", backend)
    assert key_hash(_key(plan)) == legacy_hash


def test_disabled_prefix_probe_invalidates_a_legacy_calibration():
    fresh = _key({"model": "org/m", "enable_prefix_caching": False, "dtype": "auto"})
    legacy = {key: value for key, value in fresh.items() if key != "enable_prefix_caching_cli"}
    assert key_hash(legacy) == "30d87bc00465b934c327e2dd3a95c33ab4db557afed99c97668599bf2d6a07df"
    assert key_hash(fresh) != key_hash(legacy)
    registry = ModelProfileRegistry()
    registry.replace_from_sync({"org/m": {"base_residency_mb": 1000.0, "sync_revision": 1, "calibration_id": 11}})
    registry.set_calibration_keys({"org/m": legacy})
    assert registry.get_profile("org/m").calibration_stale is not True
    assert registry.set_calibration_keys({"org/m": fresh}) == ["org/m"]
    assert registry.get_profile("org/m").calibration_stale is True
