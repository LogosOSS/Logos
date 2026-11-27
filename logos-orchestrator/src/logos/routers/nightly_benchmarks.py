"""Admin-only nightly benchmark activation and status."""

import asyncio
from dataclasses import asdict
from datetime import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

import logos.main as main
from logos.benchmarks import nightly_proposer_config as proposer
from logos.benchmarks import nightly_store as store
from logos.benchmarks import optimization, recent_requests, workloads
from logos.benchmarks.deployment import deployment_snapshot
from logos.benchmarks.guidellm_runner import extract_serving_configuration
from logos.benchmarks.nightly import baseline_settings, local_now, schedule_window
from logos.benchmarks.nightly_overview import overview
from logos.benchmarks.reservations import benchmark_reservations
from logos.dbutils.dbmanager import DBManager
from logos.dbutils.dbrequest import (
    BenchmarkWorkloadRequest,
    NightlyProposerModelRequest,
    NightlySelectionStatusRequest,
    NightlyUpdateRequest,
    NightlyWindowRequest,
    RecentBenchmarkWorkloadRequest,
)
from logos.maintenance_window import MaintenanceWindow, maintenance_node_limit
from logos.routers.internal import _require_internal_secret

router = APIRouter(prefix="/internal/model_benchmarks/nightly", tags=["admin"])


def target_for(pair_id):
    with DBManager() as db:
        target = db.get_model_provider_benchmark_target(pair_id)
    if not target or target["provider_type"] != "logosnode":
        raise HTTPException(status_code=400, detail="Nightly benchmarks require a Logos worker")
    return target


def worker_ready(snapshot, online):
    return online and bool(((snapshot or {}).get("runtime") or {}).get("supports_nightly_gpu_budget"))


@router.post("/status")
async def nightly_status(data: NightlySelectionStatusRequest, request: Request):
    _require_internal_secret(request)
    target = target_for(data.model_provider_id)
    result = store.status(data.model_provider_id)
    window = schedule_window(result)
    result["workload_policy"] = recent_requests.policy(local_now())
    snapshot = main._logosnode_registry.peek_runtime_snapshot(target["provider_id"])
    current = extract_serving_configuration(snapshot, target["model_name"])
    scheduler = main._nightly_scheduler
    preview = (
        scheduler.preview_configuration(data.model_provider_id, target["provider_id"], target["model_name"], snapshot)
        if scheduler is not None
        and not result["recovery_required"]
        and result["status"] not in {"preparing", "running"}
        else None
    )
    baseline = current if preview is None else preview
    search = None
    if result.get("workload_id"):
        workload = workloads.load(result["workload_id"], target["model_id"])
        source = (result.get("optimizer") or {}).get("source")
        if source and source.get("username") == recent_requests.username():
            workload["source_selection"] = source
        search = optimization.context(baseline, snapshot or {}, workload, result.get("optimizer") or {})
    # Later configurations depend on measurements and are recorded in the report as they run.
    result["plan"] = [baseline_settings(baseline, workload if search else None).model_dump()]
    result["workloads"] = workloads.list_for_model(target["model_id"]) if "model_id" in target else []
    if search and not any(item["id"] == workload["id"] for item in result["workloads"]):
        result["workloads"].append(workloads.metadata(workload))
    result["model_id"] = target.get("model_id")
    result["active_trial"] = None
    worker_session = (snapshot or {}).get("session_id")
    provider_id, model_name = target["provider_id"], target["model_name"]
    if result["status"] == "running" and result["recovery_required"] and worker_session:
        job_id = benchmark_reservations.owner(provider_id, model_name, exact=True)
        if job_id and job_id > 0 and benchmark_reservations.is_nightly_reserved(provider_id, model_name):
            result["active_trial"] = store.active_trial(
                data.model_provider_id, provider_id, job_id, str(worker_session)
            )
    with DBManager() as db:
        active_benchmark = db.session.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM jobs WHERE environment = 'model-provider-benchmark' "
                "AND status IN ('pending', 'running') "
                "AND (request_payload->>'provider_id')::integer = :provider_id)"
            ),
            {"provider_id": target["provider_id"]},
        ).scalar()
    online = main._logosnode_registry.is_provider_online(target["provider_id"])
    result["worker_ready"] = worker_ready(snapshot, online)
    result["deployment"] = deployment_snapshot(
        snapshot,
        target["model_name"],
        online=online,
        series_key=search["series_key"] if search else None,
        maintenance=bool(active_benchmark)
        or result["recovery_required"]
        or result["status"] in {"preparing", "running", "recovery_required"},
    )
    if result["deployment"]["configuration"] != baseline:
        result["deployment"]["series_key"] = None
    result["selection"] = store.selection_summary(
        data.model_provider_id, result.get("workload_id"), length_comparison_id=data.length_comparison_id
    )
    result["window"] = window.describe()
    result["timezone"] = result["window"]["timezone"]
    result["max_maintenance_nodes"] = maintenance_node_limit()
    return result


@router.post("/configure")
async def configure_nightly(data: NightlyUpdateRequest, request: Request):
    _require_internal_secret(request)
    target = target_for(data.model_provider_id)
    if data.enabled:
        snapshot = main._logosnode_registry.peek_runtime_snapshot(target["provider_id"]) or {}
        if not worker_ready(snapshot, main._logosnode_registry.is_provider_online(target["provider_id"])):
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
        result["window"] = schedule_window(result).describe()
        result["workload_policy"] = recent_requests.policy()
        result["max_maintenance_nodes"] = maintenance_node_limit()
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/window")
async def configure_nightly_window(data: NightlyWindowRequest, request: Request):
    _require_internal_secret(request)
    target_for(data.model_provider_id)
    timezone = MaintenanceWindow.from_env().timezone
    try:
        window = MaintenanceWindow(time.fromisoformat(data.start), time.fromisoformat(data.end), timezone)
        expected = MaintenanceWindow(
            time.fromisoformat(data.expected_start), time.fromisoformat(data.expected_end), timezone
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not store.set_window(data.model_provider_id, window, expected):
        raise HTTPException(status_code=409, detail="The nightly window changed. Reopen the editor and try again.")
    return {"window": window.describe()}


@router.post("/workloads")
async def create_workload(data: BenchmarkWorkloadRequest | RecentBenchmarkWorkloadRequest, request: Request):
    _require_internal_secret(request)
    try:
        if isinstance(data, RecentBenchmarkWorkloadRequest):
            return await run_in_threadpool(
                workloads.create_recent, target_for(data.model_provider_id), data.lookback_days
            )
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


async def _logos_prod_catalog() -> dict | None:
    gateway = proposer.LogosProdGateway.from_env()
    if gateway is None:
        return None
    try:
        async with asyncio.timeout(proposer.CLOUD_CATALOG_TIMEOUT):
            return {"models": await gateway.models(), "available": True}
    except (httpx.HTTPError, ValueError, TimeoutError):
        return {"models": [], "available": False}


@router.post("/proposer")
async def nightly_proposer_settings(request: Request):
    _require_internal_secret(request)
    selection, local, remote = await asyncio.gather(
        run_in_threadpool(proposer.selection),
        run_in_threadpool(proposer.local_models),
        _logos_prod_catalog(),
    )
    return {"selection": asdict(selection), "local_models": local, "logos_prod": remote}


@router.post("/proposer/configure")
async def configure_nightly_proposer(data: NightlyProposerModelRequest, request: Request):
    _require_internal_secret(request)
    try:
        async with asyncio.timeout(proposer.CLOUD_CATALOG_TIMEOUT):
            options = await proposer.catalog(data.provider)
    except (httpx.HTTPError, ValueError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail="Could not load the selected provider's models") from exc
    if data.model not in options:
        raise HTTPException(status_code=400, detail="This model is unavailable from the selected provider")
    saved = await run_in_threadpool(proposer.save_selection, data.provider, data.model, data.revision)
    if saved is None:
        raise HTTPException(status_code=409, detail="This selection changed. Reload it and try again.")
    return asdict(saved)
