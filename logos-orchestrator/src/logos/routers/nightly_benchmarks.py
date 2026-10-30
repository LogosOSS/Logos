"""Admin-only nightly benchmark activation and status."""

from fastapi import APIRouter, HTTPException, Request

import logos.main as main
from logos.benchmarks import nightly_store as store
from logos.benchmarks import optimization, recent_requests, workloads
from logos.benchmarks.guidellm_runner import extract_serving_configuration
from logos.benchmarks.nightly import local_now, parameter_plan
from logos.benchmarks.nightly_overview import overview
from logos.dbutils.dbmanager import DBManager
from logos.dbutils.dbrequest import BenchmarkWorkloadRequest, NightlyStatusRequest, NightlyUpdateRequest
from logos.maintenance_window import MaintenanceWindow, maintenance_node_limit
from logos.routers.internal import _require_internal_secret

router = APIRouter(prefix="/internal/model_benchmarks/nightly", tags=["admin"])


def target_for(pair_id):
    with DBManager() as db:
        target = db.get_model_provider_benchmark_target(pair_id)
    if not target or target["provider_type"] != "logosnode":
        raise HTTPException(status_code=400, detail="Nightly benchmarks require a Logos worker")
    return target


@router.post("/status")
async def nightly_status(data: NightlyStatusRequest, request: Request):
    _require_internal_secret(request)
    target = target_for(data.model_provider_id)
    result = store.status(data.model_provider_id)
    result["workload_policy"] = recent_requests.policy(local_now())
    snapshot = main._logosnode_registry.peek_runtime_snapshot(target["provider_id"])
    current = extract_serving_configuration(snapshot, target["model_name"])
    search = None
    if result.get("workload_id"):
        workload = workloads.load(result["workload_id"], target["model_id"])
        source = (result.get("optimizer") or {}).get("source")
        if source and source.get("username") == recent_requests.username():
            workload["source_selection"] = source
        search = optimization.context(current, snapshot or {}, workload, result.get("optimizer") or {})
    result["plan"] = [
        run.model_dump()
        for run in (
            optimization.plan(
                current, search, round_index=optimization.round_index(search["state"], local_now().date())
            )
            if search
            else parameter_plan(current)
        )
    ]
    result["workloads"] = workloads.list_for_model(target["model_id"]) if "model_id" in target else []
    if search and not any(item["id"] == workload["id"] for item in result["workloads"]):
        result["workloads"].append(workloads.metadata(workload))
    result["model_id"] = target.get("model_id")
    result["window"] = MaintenanceWindow.from_env().describe()
    result["timezone"] = result["window"]["timezone"]
    result["max_maintenance_nodes"] = maintenance_node_limit()
    return result


@router.post("/configure")
async def configure_nightly(data: NightlyUpdateRequest, request: Request):
    _require_internal_secret(request)
    target = target_for(data.model_provider_id)
    if data.enabled:
        snapshot = main._logosnode_registry.peek_runtime_snapshot(target["provider_id"]) or {}
        if not (snapshot.get("runtime") or {}).get("supports_benchmark_restart"):
            raise HTTPException(status_code=409, detail="Update and connect this worker before enabling nightly runs")
    try:
        if "workload_id" in data.model_fields_set:
            if recent_requests.username():
                raise ValueError("This deployment freezes the configured user's latest 50 FULL requests each night")
            if data.workload_id:
                workloads.load(data.workload_id, target["model_id"])
            result = store.set_enabled(
                data.model_provider_id, data.enabled, data.expected_enabled, data.workload_id, data.expected_workload_id
            )
        else:
            result = store.set_enabled(data.model_provider_id, data.enabled, data.expected_enabled)
        result["window"] = MaintenanceWindow.from_env().describe()
        result["workload_policy"] = recent_requests.policy()
        result["max_maintenance_nodes"] = maintenance_node_limit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workloads")
async def create_workload(data: BenchmarkWorkloadRequest, request: Request):
    _require_internal_secret(request)
    try:
        return workloads.create(data.log_ids, data.name, data.provenance)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/overview")
async def nightly_overview(request: Request):
    _require_internal_secret(request)
    if main._nightly_scheduler is None:
        raise HTTPException(status_code=503, detail="Nightly scheduler is starting")
    now = local_now()
    return overview(
        store.overview_rows(),
        main._nightly_scheduler,
        now,
        recovery_pending=store.has_recovery(),
        claimed_count=store.claimed_today(now.date()),
    )
