from contextlib import nullcontext
from datetime import time
from importlib import import_module
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Request

from logos.benchmarks import optimization
from logos.benchmarks.configuration import QWEN_MTP_MODEL
from logos.benchmarks.guidellm_runner import extract_serving_configuration
from logos.benchmarks.nightly import CAPACITY_WAITING
from logos.benchmarks.worker_limits import nightly_tuning_configuration, worker_limits
from logos.dbutils.dbrequest import (
    NightlyProposerModelRequest,
    NightlySelectionStatusRequest,
    NightlyUpdateRequest,
    NightlyWindowRequest,
)
from logos.maintenance_window import MaintenanceWindow

TARGET = {"provider_id": 20, "provider_type": "logosnode", "model_id": 11, "model_name": "model"}
LIVE = {"max_num_seqs": 12, "max_num_batched_tokens": 2048}
PREVIEW = {"max_num_seqs": 20, "max_num_batched_tokens": 4096}
WORKLOAD = {"id": "a" * 64, "name": "Frozen requests", "request_count": 10, "provenance": "synthetic"}


@pytest.fixture
def routes(monkeypatch):
    main = import_module("logos.main")
    router = import_module("logos.routers.nightly_benchmarks")
    monkeypatch.setattr(import_module("logos.routers.internal"), "_INTERNAL_SECRET", "test-only")
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer test-only")]})
    registry = Mock()
    registry.is_provider_online.return_value = True
    registry.peek_runtime_snapshot.return_value = {
        "session_id": "worker-session",
        "runtime": {
            "supports_benchmark_restart": True,
            "supports_nightly_gpu_budget": True,
            "lanes": [
                {
                    "model": "model",
                    "runtime_state": "running",
                    "lane_config": {"vllm_config": LIVE},
                }
            ],
        },
    }
    scheduler = Mock()
    scheduler.preview_configuration.return_value = PREVIEW
    monkeypatch.setattr(main, "_logosnode_registry", registry)
    monkeypatch.setattr(main, "_nightly_scheduler", scheduler)
    db = Mock()
    db.get_model_provider_benchmark_target.return_value = TARGET
    db.session.execute.return_value.scalar.return_value = False
    monkeypatch.setattr(router, "DBManager", lambda: nullcontext(db))
    return router, request, scheduler, registry


@pytest.mark.parametrize(
    ("status", "recovery_required", "use_preview"),
    [
        (CAPACITY_WAITING, False, True),
        ("preparing", False, False),
        ("running", False, False),
        ("recovery_required", True, False),
    ],
)
async def test_status_uses_a_future_plan_without_presenting_it_as_the_live_deployment(
    routes, monkeypatch, status, recovery_required, use_preview
):
    router, request, scheduler, registry = routes
    monkeypatch.setattr(
        router.store,
        "status",
        Mock(return_value={"status": status, "workload_id": WORKLOAD["id"], "recovery_required": recovery_required}),
    )
    monkeypatch.setattr(router.workloads, "load", Mock(return_value=WORKLOAD))
    monkeypatch.setattr(router.workloads, "list_for_model", Mock(return_value=[WORKLOAD]))
    selection = Mock(return_value={})
    monkeypatch.setattr(router.store, "selection_summary", selection)

    result = await router.nightly_status(
        NightlySelectionStatusRequest(model_provider_id=31, length_comparison_id=42), request
    )

    expected_plan = PREVIEW if use_preview else LIVE
    assert result["plan"][0]["serving_overrides"]["max_num_seqs"] == expected_plan["max_num_seqs"]
    assert result["plan"][0]["serving_overrides"]["max_num_batched_tokens"] == expected_plan["max_num_batched_tokens"]
    assert result["deployment"]["configuration"] == LIVE
    assert result["deployment"]["maintenance"] is not use_preview
    assert bool(result["deployment"]["series_key"]) is not use_preview
    assert result["worker_ready"] is True
    assert result["active_trial"] is None
    selection.assert_called_once_with(31, WORKLOAD["id"], length_comparison_id=42)
    if use_preview:
        scheduler.preview_configuration.assert_called_once_with(
            31, 20, "model", registry.peek_runtime_snapshot.return_value
        )
    else:
        scheduler.preview_configuration.assert_not_called()


@pytest.mark.parametrize(("job_id", "reserved"), [(41, True), (41, False), (-31, True)])
async def test_status_only_exposes_an_active_trial_from_the_nightly_reservation(routes, monkeypatch, job_id, reserved):
    router, request, _, _ = routes
    monkeypatch.setattr(
        router.store,
        "status",
        Mock(return_value={"status": "running", "recovery_required": True}),
    )
    monkeypatch.setattr(router.workloads, "list_for_model", Mock(return_value=[]))
    monkeypatch.setattr(router.store, "selection_summary", Mock(return_value={}))
    active = {"baseline_id": 71, "changes": [{"parameter": "max_num_seqs", "before": 12, "after": 6}]}
    trial = Mock(return_value=active)
    monkeypatch.setattr(router.store, "active_trial", trial)
    monkeypatch.setattr(router.benchmark_reservations, "owner", Mock(return_value=job_id))
    monkeypatch.setattr(router.benchmark_reservations, "is_nightly_reserved", Mock(return_value=reserved))

    result = await router.nightly_status(NightlySelectionStatusRequest(model_provider_id=31), request)

    if job_id > 0 and reserved:
        trial.assert_called_once_with(31, 20, 41, "worker-session")
        assert result["active_trial"] == active
    else:
        trial.assert_not_called()
        assert result["active_trial"] is None


@pytest.mark.parametrize(("reported", "effective"), [("", "auto"), ("fp8_e4m3", "fp8")])
async def test_live_deployment_and_nightly_planning_share_cache_identity(routes, monkeypatch, reported, effective):
    router, request, scheduler, registry = routes
    monkeypatch.setattr(router, "target_for", lambda _: {**TARGET, "model_name": QWEN_MTP_MODEL})
    snapshot = registry.peek_runtime_snapshot.return_value
    snapshot["runtime"]["devices"] = {
        "nvidia_smi_available": True,
        "devices": [{"kind": "nvidia", "extra": {"index": 0, "compute_capability": "12.0"}}],
    }
    snapshot["runtime"]["lanes"][0].update(
        model=QWEN_MTP_MODEL,
        nightly_vllm_version="0.34.0",
        lane_config={"vllm": True, "gpu_devices": "0", "vllm_config": {**LIVE, "kv_cache_dtype": reported}},
    )
    serving = extract_serving_configuration(snapshot, QWEN_MTP_MODEL)
    scheduler.preview_configuration.return_value = serving
    tuning = nightly_tuning_configuration(worker_limits(snapshot, QWEN_MTP_MODEL))
    assert tuning["kv_cache_dtype"] == effective
    measured = optimization.context(tuning, snapshot, WORKLOAD, {})
    monkeypatch.setattr(
        router.store,
        "status",
        Mock(
            return_value={
                "status": "completed",
                "workload_id": WORKLOAD["id"],
                "recovery_required": False,
                "optimizer": measured["state"],
            }
        ),
    )
    monkeypatch.setattr(router.workloads, "load", Mock(return_value=WORKLOAD))
    monkeypatch.setattr(router.workloads, "list_for_model", Mock(return_value=[WORKLOAD]))
    monkeypatch.setattr(router.store, "selection_summary", Mock(return_value={}))

    result = await router.nightly_status(NightlySelectionStatusRequest(model_provider_id=31), request)

    assert result["deployment"]["series_key"] == measured["series_key"]
    assert result["deployment"]["configuration"]["kv_cache_dtype"] == reported


async def test_only_gpu_budget_capable_workers_can_enable_nightly_rotation(routes, monkeypatch):
    router, request, _, registry = routes
    snapshot = registry.peek_runtime_snapshot.return_value
    del snapshot["runtime"]["supports_nightly_gpu_budget"]
    set_enabled = Mock(return_value={"enabled": True})
    monkeypatch.setattr(router.store, "set_enabled", set_enabled)
    data = NightlyUpdateRequest(model_provider_id=31, enabled=True, expected_enabled=False)

    with pytest.raises(HTTPException) as exc:
        await router.configure_nightly(data, request)
    assert exc.value.status_code == 409
    set_enabled.assert_not_called()

    snapshot["runtime"]["supports_nightly_gpu_budget"] = True
    result = await router.configure_nightly(data, request)
    assert result["enabled"] is True
    set_enabled.assert_called_once_with(31, True, False)


async def test_window_endpoint_reports_the_persisted_pair_window(routes, monkeypatch):
    router, request, _, _ = routes
    saved = Mock(return_value=True)
    monkeypatch.setattr(router.store, "set_window", saved)
    data = NightlyWindowRequest(
        model_provider_id=31, start="13:10", end="13:30", expected_start="05:00", expected_end="08:00"
    )
    zone = MaintenanceWindow.from_env().timezone
    window = MaintenanceWindow(time(13, 10), time(13, 30), zone)

    assert await router.configure_nightly_window(data, request) == {"window": window.describe()}
    saved.assert_called_once_with(31, window, MaintenanceWindow(time(5), time(8), zone))


@pytest.mark.parametrize(("end", "status"), [("13:20", 400), ("13:30", 409)])
async def test_window_endpoint_rejects_short_and_stale_updates(routes, monkeypatch, end, status):
    router, request, _, _ = routes
    saved = Mock(return_value=False)
    monkeypatch.setattr(router.store, "set_window", saved)
    data = NightlyWindowRequest(
        model_provider_id=31, start="13:10", end=end, expected_start="05:00", expected_end="08:00"
    )

    with pytest.raises(HTTPException) as exc:
        await router.configure_nightly_window(data, request)

    assert exc.value.status_code == status
    assert saved.call_count == (1 if status == 409 else 0)


async def test_proposer_settings_keeps_local_catalog_when_prod_is_unreachable(routes, monkeypatch):
    router, request, _, _ = routes
    config = router.proposer
    monkeypatch.setattr(config, "selection", Mock(return_value=config.ProposerModel("local", "local/model")))
    monkeypatch.setattr(config, "local_models", Mock(return_value=["local/model"]))
    remote = Mock(models=AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(config.LogosProdGateway, "from_env", lambda: remote)

    assert await router.nightly_proposer_settings(request) == {
        "selection": {"provider": "local", "model": "local/model", "revision": None},
        "local_models": ["local/model"],
        "logos_prod": {"available": False, "models": []},
    }


async def test_proposer_update_validates_the_catalog_and_revision(routes, monkeypatch):
    router, request, _, _ = routes
    config = router.proposer
    catalog = AsyncMock(return_value=["prod/model"])
    save = Mock(return_value=config.ProposerModel("logos_prod", "prod/model", 2))
    monkeypatch.setattr(config, "catalog", catalog)
    monkeypatch.setattr(config, "save_selection", save)
    data = NightlyProposerModelRequest(provider="logos_prod", model="prod/model", revision=1)

    assert await router.configure_nightly_proposer(data, request) == {
        "provider": "logos_prod",
        "model": "prod/model",
        "revision": 2,
    }
    catalog.assert_awaited_once_with("logos_prod")
    save.assert_called_once_with("logos_prod", "prod/model", 1)

    save.return_value = None
    with pytest.raises(HTTPException) as stale:
        await router.configure_nightly_proposer(data, request)
    assert stale.value.status_code == 409

    save.reset_mock()
    catalog.return_value = []
    with pytest.raises(HTTPException) as unavailable:
        await router.configure_nightly_proposer(data, request)
    assert unavailable.value.status_code == 400
    save.assert_not_called()
