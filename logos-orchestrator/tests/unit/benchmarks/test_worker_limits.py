import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from logos.benchmarks.configuration import NGRAM_CONFIG, QWEN_MTP_CONFIG, QWEN_MTP_MODEL, ServingOverrides
from logos.benchmarks.worker_limits import (
    nightly_tuning_configuration,
    nightly_variation_values,
    validate_worker_overrides,
    worker_limits,
)


def snapshot(count=2, selector="all"):
    return {
        "last_heartbeat": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "first_status_received": True,
        "runtime": {
            "gpu_devices": selector,
            "devices": {
                "nvidia_smi_available": True,
                "devices": [{"kind": "nvidia", "memory_total_mb": 24000} for _ in range(count)],
            },
            "lanes": [{"model": "m", "lane_config": {"vllm_config": {"tensor_parallel_size": 1}}}],
        },
    }


@pytest.mark.parametrize("count,tp,pp", [(2, 1, 1), (2, 2, 1), (2, 1, 2), (3, 3, 1)])
def test_parallelism_within_live_worker_gpu_count(count, tp, pp):
    validate_worker_overrides(
        ServingOverrides(tensor_parallel_size=tp, pipeline_parallel_size=pp), worker_limits(snapshot(count), "m")
    )


@pytest.mark.parametrize("tp,pp", [(38, 1), (3, 1), (2, 2), (1, 3)])
def test_impossible_parallelism_is_rejected(tp, pp):
    with pytest.raises(ValueError, match="only 2 available"):
        validate_worker_overrides(
            ServingOverrides(tensor_parallel_size=tp, pipeline_parallel_size=pp), worker_limits(snapshot(), "m")
        )


def test_limits_respect_worker_and_lane_gpu_assignments():
    status = snapshot(3, "1,2")
    status["runtime"]["lanes"][0]["is_static"] = True
    status["runtime"]["lanes"][0]["lane_config"]["gpu_devices"] = "2"
    assert worker_limits(status, "m")["gpu_count"] == 1


def nightly_snapshot(model=QWEN_MTP_MODEL, *, version="0.34.0", capability="12.0", blocked=(), config=None):
    status = snapshot()
    status["runtime"]["devices"]["devices"][0]["extra"] = {"index": 0, "compute_capability": capability}
    status["runtime"]["lanes"][0].update(
        model=model,
        nightly_vllm_version=version,
        nightly_prefix_caching_checked=True,
        nightly_blocked_vllm_fields=list(blocked),
        lane_config={"vllm": True, "gpu_devices": "0", "vllm_config": config or {}},
    )
    return status


def test_nightly_values_use_the_lane_gpus_and_resolve_tuning_defaults():
    limits = worker_limits(nightly_snapshot(), QWEN_MTP_MODEL)
    assert nightly_variation_values(limits) == {
        "kv_cache_dtype": ("auto", "fp8"),
        "speculative_config": ("", QWEN_MTP_CONFIG),
    }
    assert "kv_cache_dtype" not in limits["current"]
    assert "speculative_config" not in limits["current"]
    assert nightly_tuning_configuration(limits)["kv_cache_dtype"] == "auto"
    assert nightly_tuning_configuration(limits)["speculative_config"] == ""
    validate_worker_overrides(ServingOverrides(speculative_config=QWEN_MTP_CONFIG), limits)
    validate_worker_overrides(ServingOverrides(speculative_config=""), limits)


@pytest.mark.parametrize("size", [7, 14])
def test_smaller_qwen_variants_offer_cache_precision_and_ngram_without_mtp(size):
    model = f"Qwen/Qwen2.5-Coder-{size}B-Instruct-AWQ"
    limits = worker_limits(nightly_snapshot(model), model)
    assert nightly_variation_values(limits) == {
        "kv_cache_dtype": ("auto", "fp8"),
        "speculative_config": ("", NGRAM_CONFIG),
    }
    with pytest.raises(ValueError, match="does not support"):
        validate_worker_overrides(ServingOverrides(speculative_config=QWEN_MTP_CONFIG), limits)


@pytest.mark.parametrize("size", [7, 14])
def test_turing_qwen_offers_prefix_caching_and_ngram(size):
    model = f"Qwen/Qwen2.5-Coder-{size}B-Instruct-AWQ"
    config = {"attention_backend": "TRITON_ATTN", "enable_prefix_caching": True, "speculative_config": ""}
    status = nightly_snapshot(model, capability="7.5", config=config)
    limits = worker_limits(status, model)
    assert nightly_variation_values(limits) == {
        "enable_prefix_caching": (False, True),
        "speculative_config": ("", NGRAM_CONFIG),
    }
    overrides = ServingOverrides(enable_prefix_caching=False, speculative_config=NGRAM_CONFIG)
    validate_worker_overrides(overrides, limits, nightly=True)
    # Restart validation must use the original config even when the live lane is already running the trial.
    config.update(enable_prefix_caching=False, speculative_config=NGRAM_CONFIG)
    assert nightly_variation_values(worker_limits(status, model)) == nightly_variation_values(limits)
    validate_worker_overrides(
        ServingOverrides(enable_prefix_caching=True, speculative_config=""),
        worker_limits(
            status,
            model,
            baseline_config=config,
        ),
        nightly=True,
    )


@pytest.mark.parametrize("checked,blocked", [(False, ()), (True, ("enable_prefix_caching",))])
def test_nightly_prefix_override_requires_unambiguous_worker_telemetry(checked, blocked):
    model = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
    status = nightly_snapshot(model, capability="7.5", blocked=blocked, config={"enable_prefix_caching": True})
    status["runtime"]["lanes"][0]["nightly_prefix_caching_checked"] = checked
    limits = worker_limits(status, model)
    assert nightly_variation_values(limits) == {"speculative_config": ("", NGRAM_CONFIG)}
    with pytest.raises(ValueError, match="prefix caching"):
        validate_worker_overrides(ServingOverrides(enable_prefix_caching=False), limits, nightly=True)
    validate_worker_overrides(ServingOverrides(enable_prefix_caching=False), limits)


@pytest.mark.parametrize(
    "settings",
    [
        {"version": None},
        {"version": "0.22.0"},
        {"capability": "7.5"},
        {"blocked": ("speculative_config", "kv_cache_dtype")},
        {"blocked": ("speculative_config",)},
        {"config": {"speculative_config": '{"method":"ngram","num_speculative_tokens":2}'}},
        {"config": {"attention_backend": "TURBOQUANT", "kv_cache_dtype": "turboquant_k8v4"}},
    ],
)
def test_nightly_extra_values_require_reported_compatibility(settings):
    limits = worker_limits(nightly_snapshot(**settings), QWEN_MTP_MODEL)
    assert nightly_variation_values(limits) == {}


def test_nightly_limits_with_explicit_baseline_do_not_inherit_last_candidate():
    status = nightly_snapshot(config={"kv_cache_dtype": "fp8", "speculative_config": QWEN_MTP_CONFIG})
    limits = worker_limits(status, QWEN_MTP_MODEL, baseline_config={})
    assert "kv_cache_dtype" not in limits["current"]
    assert "speculative_config" not in limits["current"]
    assert nightly_tuning_configuration(limits)["kv_cache_dtype"] == "auto"
    assert nightly_tuning_configuration(limits)["speculative_config"] == ""
    assert worker_limits(status, QWEN_MTP_MODEL)["current"]["speculative_config"] == QWEN_MTP_CONFIG


def test_nightly_cache_recognizes_the_fp8_e4m3_alias_on_an_older_vllm_release():
    status = nightly_snapshot(version="0.26.1rc1", config={"kv_cache_dtype": "fp8_e4m3"})
    limits = worker_limits(status, QWEN_MTP_MODEL)
    assert nightly_variation_values(limits)["kv_cache_dtype"] == ("auto", "fp8")
    assert limits["current"]["kv_cache_dtype"] == "fp8_e4m3"
    assert nightly_tuning_configuration(limits)["kv_cache_dtype"] == "fp8"


def test_combination_includes_current_settings_when_only_one_field_changes():
    status = snapshot()
    status["runtime"]["lanes"][0]["lane_config"]["vllm_config"]["tensor_parallel_size"] = 2
    with pytest.raises(ValueError, match="requires 4 GPUs"):
        validate_worker_overrides(ServingOverrides(pipeline_parallel_size=2), worker_limits(status, "m"))


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"kv_cache_memory_bytes": "0"}, "greater than zero"),
        ({"kv_cache_memory_bytes": "999G"}, "smaller than its total memory"),
        ({"max_num_seqs": 32, "max_num_batched_tokens": 8}, "at least max sequences"),
        ({"attention_backend": "TURBOQUANT"}, "requires a turboquant KV cache dtype"),
    ],
)
def test_known_memory_and_batch_limits(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_worker_overrides(ServingOverrides(**overrides), worker_limits(snapshot(), "m"))


@pytest.mark.parametrize("cache", ["turboquant_k8v4", "turboquant_4bit_nc", "turboquant_k3v4_nc", "turboquant_3bit_nc"])
def test_turboquant_uses_explicit_or_existing_cache_setting(cache):
    limits = worker_limits(snapshot(), "m")
    validate_worker_overrides(ServingOverrides(attention_backend="TURBOQUANT", kv_cache_dtype=cache), limits)
    limits["current"]["kv_cache_dtype"] = cache
    validate_worker_overrides(ServingOverrides(attention_backend="TURBOQUANT"), limits)
    limits["current"]["attention_backend"] = "TURBOQUANT"
    with pytest.raises(ValueError, match="requires a turboquant"):
        validate_worker_overrides(ServingOverrides(kv_cache_dtype="auto"), limits)


def test_unknown_gpu_inventory_does_not_invent_a_limit():
    limits = worker_limits(None, "m")
    assert limits["gpu_count"] is None
    validate_worker_overrides(ServingOverrides(), limits)
    with pytest.raises(ValueError, match="GPU information is unavailable"):
        validate_worker_overrides(ServingOverrides(tensor_parallel_size=1), limits)


@pytest.mark.parametrize("batch", [False, True])
async def test_impossible_benchmark_is_rejected_before_job_creation(monkeypatch, batch):
    from logos.benchmarks import nightly_store

    monkeypatch.setattr(nightly_store, "manual_admission", lambda db, provider_id: True)
    import importlib

    main = importlib.import_module("logos.main")
    internal = importlib.import_module("logos.routers.internal")

    db = MagicMock()
    db.__enter__.return_value = db
    db.get_model_provider_benchmark_target.return_value = {
        "provider_id": 7,
        "provider_type": "logosnode",
        "provider_name": "two-gpu-worker",
        "model_name": "m",
    }
    db.find_active_model_benchmark_job.return_value = None
    monkeypatch.setattr(internal, "DBManager", lambda: db)
    monkeypatch.setattr(main, "_capacity_planner", MagicMock())
    monkeypatch.setattr(internal, "_require_internal_secret", lambda _: None)
    registry = MagicMock()
    registry.peek_runtime_snapshot.return_value = snapshot()
    monkeypatch.setattr(main, "_logosnode_registry", registry)
    monkeypatch.setattr(internal, "dataset_metadata", AsyncMock(return_value={"text_columns": ["question"]}))
    with pytest.raises(HTTPException, match="only 2 available") as error:
        await internal.internal_run_model_benchmark(
            internal.InternalBenchmarkRequest(
                model_provider_id=31,
                **(
                    {
                        "batch": {
                            "configurations": [
                                {"serving_overrides": {"tensor_parallel_size": 1}},
                                {"serving_overrides": {"tensor_parallel_size": 38}},
                            ]
                        }
                    }
                    if batch
                    else {"serving_overrides": {"tensor_parallel_size": 38}}
                ),
            ),
            MagicMock(),
        )
    assert error.value.status_code == 400
    db.create_job_record.assert_not_called()


@pytest.mark.parametrize("backend", ["FLASHINFER", "FLASH_ATTN"])
def test_pre_ampere_attention_rejected_before_worker_preparation(backend):
    status = snapshot()
    status["runtime"]["devices"]["devices"][0].update(
        name="Quadro RTX 5000", extra={"index": 0, "compute_capability": "7.5"}
    )
    limits = worker_limits(status, "m")
    assert limits["gpus"][0]["compute_capability"] == "7.5"
    with pytest.raises(ValueError, match="requires Ampere"):
        validate_worker_overrides(ServingOverrides(attention_backend=backend), limits)
    validate_worker_overrides(ServingOverrides(attention_backend="TRITON_ATTN"), limits)


@pytest.mark.parametrize("cap", [None, "N/A", "", "8.0", "8.9", "9.0", "10.0"])
def test_unknown_or_newer_architecture_is_not_claimed_incompatible(cap):
    status = snapshot(1)
    status["runtime"]["devices"]["devices"][0]["extra"] = {"compute_capability": cap}
    assert worker_limits(status, "m")["attention_backend_restrictions"] == {}


def test_attention_restrictions_respect_selected_gpus_and_current_backend():
    status = snapshot(2, "1")
    for index, cap in enumerate(["7.5", "8.0"]):
        status["runtime"]["devices"]["devices"][index]["extra"] = {"index": index, "compute_capability": cap}
    assert worker_limits(status, "m")["attention_backend_restrictions"] == {}
    status["runtime"]["gpu_devices"] = "all"
    limits = worker_limits(status, "m")
    limits["current"]["attention_backend"] = "FLASHINFER"
    with pytest.raises(ValueError, match="requires Ampere"):
        validate_worker_overrides(ServingOverrides(tensor_parallel_size=1), limits)
    with pytest.raises(ValueError, match="requires Ampere"):
        validate_worker_overrides(ServingOverrides(), limits)


@pytest.mark.parametrize("backend", ["FLASHINFER", "FLASH_ATTN", "TRITON_ATTN", "FLEX_ATTENTION"])
def test_turboquant_cache_rejects_other_explicit_backends(backend):
    limits = worker_limits(snapshot(), "m")
    limits["current"]["kv_cache_dtype"] = "turboquant_k8v4"
    with pytest.raises(ValueError, match="TurboQuant KV cache requires"):
        validate_worker_overrides(ServingOverrides(attention_backend=backend), limits)
    limits["current"] = {"attention_backend": backend}
    with pytest.raises(ValueError, match="TurboQuant KV cache requires"):
        validate_worker_overrides(ServingOverrides(kv_cache_dtype="turboquant_k8v4"), limits)


def metal_snapshot():
    status = snapshot()
    status["runtime"]["devices"] = {
        "mode": "metal",
        "nvidia_smi_available": False,
        "telemetry_available": True,
        "devices": [{"kind": "metal", "memory_total_mb": 24000}],
    }
    status["runtime"]["lanes"][0]["lane_config"]["vllm_config"] = {
        "max_num_seqs": 16,
        "max_num_batched_tokens": 4096,
    }
    return status


def test_measured_metal_supports_batching_without_inventing_nvidia_inventory():
    limits = worker_limits(metal_snapshot(), "m")
    assert limits["gpu_count"] is None
    validate_worker_overrides(ServingOverrides(max_num_seqs=20, max_num_batched_tokens=5120), limits)
    with pytest.raises(ValueError, match="at least max sequences"):
        validate_worker_overrides(ServingOverrides(max_num_batched_tokens=8), limits)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tensor_parallel_size": 1},
        {"pipeline_parallel_size": 2},
        {"gpu_memory_utilization": 0.8},
        {"kv_cache_memory_bytes": "4G"},
        {"attention_backend": "FLASH_ATTN"},
    ],
)
def test_metal_rejects_gpu_topology_memory_and_attention_overrides(overrides):
    with pytest.raises(ValueError, match="batching overrides only"):
        validate_worker_overrides(ServingOverrides(**overrides), worker_limits(metal_snapshot(), "m"))


@pytest.mark.parametrize(
    "change",
    [
        {"telemetry_available": False},
        {"degraded_reason": "mlx unavailable"},
        {"devices": []},
        {"mode": "derived"},
    ],
)
def test_missing_or_estimated_metal_telemetry_does_not_allow_tuning(change):
    status = metal_snapshot()
    status["runtime"]["devices"].update(change)
    with pytest.raises(ValueError, match="GPU information is unavailable"):
        validate_worker_overrides(ServingOverrides(max_num_seqs=20), worker_limits(status, "m"))


@pytest.mark.parametrize("nightly", [False, True])
async def test_metal_batching_is_admitted_through_internal_benchmark_route(monkeypatch, nightly):
    import importlib

    from logos.benchmarks import nightly_store

    main = importlib.import_module("logos.main")
    internal = importlib.import_module("logos.routers.internal")
    db = MagicMock()
    db.__enter__.return_value = db
    db.find_active_model_benchmark_job.return_value = None
    monkeypatch.setattr(internal, "_INTERNAL_SECRET", "test-secret")
    db.create_job_record.return_value = 712
    db.get_model_provider_benchmark_target.return_value = {
        "provider_id": 7,
        "provider_type": "logosnode",
        "provider_name": "Metal worker",
        "model_id": 1,
        "model_name": "m",
    }
    monkeypatch.setattr(nightly_store, "manual_admission", lambda db, provider_id: True)
    monkeypatch.setattr(main, "_maintenance_coordinator", None)
    monkeypatch.setattr(main, "_calibration_orchestrator", None)
    registry = MagicMock()
    registry.active_provider_ids.return_value = [7]
    registry.is_calibrating.return_value = False
    status = metal_snapshot()
    lanes = status["runtime"]["lanes"]
    if nightly:
        lanes[0].update(lane_id="pinned", runtime_state="loaded")
        lanes.insert(
            0,
            {
                "lane_id": "other-replica",
                "model": "m",
                "runtime_state": "loaded",
                "lane_config": {"vllm": True, "vllm_config": {"max_num_batched_tokens": 8}},
            },
        )
    registry.peek_runtime_snapshot.return_value = status
    monkeypatch.setattr(main, "_logosnode_registry", registry)
    monkeypatch.setattr(main, "_capacity_planner", MagicMock())
    monkeypatch.setattr(internal, "DBManager", lambda: db)
    monkeypatch.setattr(internal, "_require_internal_secret", lambda _: None)
    monkeypatch.setattr(internal, "dataset_metadata", AsyncMock(return_value={"text_columns": ["question"]}))
    runner = AsyncMock(return_value=1)
    monkeypatch.setattr(internal, "run_benchmark_job", runner)
    from logos.benchmarks.nightly import NIGHTLY_CONTEXT
    from logos.benchmarks.reservations import benchmark_reservations

    monkeypatch.setattr(internal, "can_start", lambda _: True)
    monkeypatch.setattr(nightly_store, "status", lambda _: {"enabled": True})
    token = NIGHTLY_CONTEXT.set(nightly)
    if nightly:
        benchmark_reservations.reserve_nightly(7, "m", -31)
        benchmark_reservations.pin_lane(7, "m", -31, "pinned")
    try:
        request_data = internal.InternalBenchmarkRequest(model_provider_id=31, serving_overrides={"max_num_seqs": 20})
        response = await internal.internal_run_model_benchmark(request_data, MagicMock())
        assert response.status_code == 202
        await main._benchmark_tasks_by_job[712]
        run = runner.await_args.kwargs
        assert run["serving_configuration"]["max_num_batched_tokens"] == 4096
        lanes[-1]["lane_config"]["vllm_config"]["max_num_batched_tokens"] = 8192
        assert run["serving_configuration_getter"]()["max_num_batched_tokens"] == 8192
        if nightly:
            lanes.pop()
            with pytest.raises(RuntimeError, match="'pinned'.*missing from worker status"):
                run["serving_configuration_getter"]()
            with pytest.raises(HTTPException, match="'pinned'.*missing from worker status") as exc:
                await internal.internal_run_model_benchmark(request_data, MagicMock())
            assert exc.value.status_code == 409
            db.create_job_record.assert_called_once()
            benchmark_reservations.release(-31)
            with pytest.raises(RuntimeError, match="no longer owns its pinned lane"):
                run["serving_configuration_getter"]()
            with pytest.raises(HTTPException, match="no longer owns its pinned lane") as exc:
                await internal.internal_run_model_benchmark(request_data, MagicMock())
            assert exc.value.status_code == 409
    finally:
        NIGHTLY_CONTEXT.reset(token)
        benchmark_reservations.release(-31)
    assert runner.await_args.kwargs["settings"].serving_overrides.max_num_seqs == 20
