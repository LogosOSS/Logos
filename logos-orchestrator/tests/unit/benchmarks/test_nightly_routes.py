from contextlib import nullcontext
from importlib import import_module
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request

from logos.benchmarks.nightly import CAPACITY_WAITING
from logos.dbutils.dbrequest import NightlySelectionStatusRequest, NightlyUpdateRequest

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
        }
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
    selection.assert_called_once_with(31, WORKLOAD["id"], length_comparison_id=42)
    if use_preview:
        scheduler.preview_configuration.assert_called_once_with(
            31, 20, "model", registry.peek_runtime_snapshot.return_value
        )
    else:
        scheduler.preview_configuration.assert_not_called()


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
