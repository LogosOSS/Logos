"""Application-owned nightly runs with a bounded number of maintenance workers."""

import asyncio
import copy
import json
import logging

from starlette.requests import Request

from logos.benchmarks import nightly_store as store
from logos.benchmarks import optimization, recent_requests, workloads
from logos.benchmarks.configuration import ServingOverrides
from logos.benchmarks.guidellm_runner import apply_serving_overrides, lane_is_serving, lane_serving_configuration
from logos.benchmarks.maintenance import AUTOMATIC_CALIBRATION
from logos.benchmarks.nightly import (
    CAPACITY_RETRY_SECONDS,
    CAPACITY_WAITING,
    LEASE_CHECK,
    MAX_PAIRS_PER_NIGHT,
    NIGHTLY_CONTEXT,
    NIGHTLY_GPU_BUDGET_REASON,
    NightlyCapacityDeferred,
    NightlyStopped,
    can_start,
    configured_lane_id,
    local_now,
    parameter_plan,
)
from logos.benchmarks.reservations import benchmark_reservations
from logos.benchmarks.worker_limits import validate_worker_overrides, worker_limits
from logos.dbutils.dbmanager import DBManager
from logos.dbutils.dbrequest import InternalBenchmarkRequest
from logos.logosnode_registry import LogosNodeCommandError
from logos.maintenance_window import maintenance_node_limit

logger = logging.getLogger(__name__)
# After add/reconfigure succeeds, wait for fresh worker status confirming the expected lane config is ready.
LANE_READY_STATUS_TIMEOUT_SECONDS = 15 * 60


class NightlyScheduler:
    def __init__(self, registry, planner, calibration=None, maintenance=None):
        self.registry = registry
        self.planner = planner
        self.task = None
        self.calibration = calibration
        self.maintenance = maintenance

    async def start(self):
        # Reinstall persisted reservations before accepting production traffic.
        for row in store.rows():
            if row["recovery"]:
                self._reserve_recovery(row["model_provider_id"], row["recovery"])
        self.task = asyncio.create_task(self._loop(), name="nightly-benchmarks")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _loop(self):
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Nightly benchmark tick failed")
            await asyncio.sleep(15)

    async def _cycle(self):
        recent_requests.freeze(local_now(), include_requests=False)
        store.finalize_continuations(local_now())
        await store.deliver_completion_events()
        claims = []
        if self.maintenance:
            completed = await self.maintenance.reconcile()
            claims = store.calibration_claims()
            if self.calibration:
                self.calibration._completed_this_window.update(completed)
                # Durable claims are authoritative after a restart or operator recovery.
                self.calibration._active_provider_ids = {claim["provider_id"] for claim in claims if claim["automatic"]}
        if self.calibration and not store.has_recovery() and not store.has_active_benchmarks():
            token = AUTOMATIC_CALIBRATION.set(True)
            try:
                await self.calibration._tick()
            finally:
                AUTOMATIC_CALIBRATION.reset(token)
        await self.tick()

    async def tick(self):
        token = NIGHTLY_CONTEXT.set(True)
        try:
            await self._tick()
        finally:
            NIGHTLY_CONTEXT.reset(token)

    async def _tick(self):
        schedules = store.rows()
        if not any(row["recovery"] for row in schedules) and not can_start(local_now()):
            return
        with store.cluster_lock() as connection:
            if connection is None:
                return
            if self.maintenance and (store.calibration_claims() or self.maintenance.live_calibration()):
                return
            for row in schedules:
                pair_id = row["model_provider_id"]
                if row["recovery"]:
                    try:
                        if not await self.recover(row, connection):
                            return
                    except Exception:
                        # Keep the node excluded and do not take a second node.
                        logger.exception("Nightly worker recovery requires attention")
                        return
            if self.calibration and self.calibration.benchmarks_must_wait():
                return
            # One scheduler owns the cluster lease. Each wave uses distinct
            # workers, and finishes cleanup before another wave can start.
            remaining = list(schedules)
            while remaining:
                selected = []
                providers = set()
                deferred = []
                for row in remaining:
                    pair_id = row["model_provider_id"]
                    day = local_now().date()
                    if (
                        not can_start(local_now())
                        or store.has_active_benchmarks()
                        or (self.calibration and self.calibration.benchmarks_must_wait())
                    ):
                        break
                    if not row["enabled"]:
                        continue
                    claimed_today = row["last_run_date"] == day
                    if claimed_today:
                        if row.get("status") == CAPACITY_WAITING:
                            updated = row.get("updated_at")
                            if updated and (local_now() - updated).total_seconds() < CAPACITY_RETRY_SECONDS:
                                continue
                        elif row.get("status") != "continuing":
                            continue
                    if not claimed_today and store.claimed_today(day) >= MAX_PAIRS_PER_NIGHT:
                        continue
                    if len(selected) >= maintenance_node_limit():
                        deferred.append(row)
                        continue
                    try:
                        target, _, _ = self.target_lane(pair_id)
                    except RuntimeError as exc:
                        # Keep claimed rounds eligible for retry or window finalization.
                        if not claimed_today:
                            store.save(pair_id, "unavailable", [{"status": "skipped", "reason": str(exc)[:1000]}], None)
                        continue
                    provider_id = int(target["provider_id"])
                    if self.planner.is_worker_locked(provider_id):
                        # The run waits for the next night the worker is not locked.
                        logger.info("Skipping nightly benchmark %s: worker %s is locked", pair_id, provider_id)
                        continue
                    if provider_id in providers:
                        deferred.append(row)
                        continue
                    if not store.lock_held(connection):
                        return
                    if store.claim(pair_id, day):
                        selected.append(pair_id)
                        providers.add(provider_id)
                if not selected:
                    return
                # TaskGroup cancels and awaits every sibling's restoration on failure or shutdown.
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(self.run_schedule(pair, connection)) for pair in selected]
                if not all(task.result() for task in tasks):
                    return
                remaining = deferred

    def healthy(self, provider_id):
        snapshot = self.registry.peek_runtime_snapshot(provider_id) or {}
        health = (snapshot.get("runtime") or {}).get("node_health") or {}
        return (
            health.get("healthy", True) is not False
            and self.registry.is_provider_online(provider_id)
            and self.registry.has_received_first_status(provider_id)
            and not self.registry.is_calibrating(provider_id)
            and not self.registry.is_cordoned(provider_id)
        )

    async def restore(self, pair_id, recovery):
        """Confirm the selected production configuration before releasing traffic."""
        if not recovery.get("mutated", True) and not recovery.get("gpu_budget"):
            return
        provider_id = recovery["provider_id"]
        if not self.healthy(provider_id):
            raise RuntimeError("Worker is offline, calibrating or cordoned; restoration is pending")
        self._reserve_recovery(pair_id, recovery)
        if recovery.get("temporary_lane") and recovery.get("mutated", True):
            if recovery.get("model_scoped"):
                benchmark_reservations.pin_lane(provider_id, recovery["model"], -pair_id, recovery["lane_id"])
            removed = await self._remove_lane(
                provider_id,
                recovery["lane_id"],
                expected_model=recovery["model"] if recovery.get("model_scoped") else None,
            )
            if removed:
                self.registry.update_desired_lane_remove(provider_id, recovery["lane_id"])
        elif recovery.get("mutated", True) and recovery.get("benchmark_lane_mutated", True):
            await self._restore_lane(recovery)
        for lane_config in recovery.get("slept_lanes", []):
            await self._restore_slept_lane(provider_id, lane_config)
        for lane_config in recovery.get("removed_lanes", []):
            await self._add_removed_lane(provider_id, lane_config)
        if recovery.get("gpu_budget"):
            self._require_lease()
            await self.registry.send_command(
                provider_id, "release_nightly_gpu_budget", {"lane_id": recovery["lane_id"]}, timeout_seconds=30
            )

    @staticmethod
    def _reserve_recovery(pair_id, recovery):
        provider_id = recovery["provider_id"]
        if recovery.get("model_scoped"):
            benchmark_reservations.reserve_nightly(provider_id, recovery["model"], -pair_id)
        else:
            # Old journals may need to restore another model displaced by a round.
            benchmark_reservations.reserve(provider_id, "*", -pair_id)

    async def _restore_lane(self, recovery, *, force_restart=False):
        recovery = copy.deepcopy(recovery)
        if recovery.get("adoption"):
            recovery["vllm_config"] = apply_serving_overrides(
                recovery["vllm_config"], ServingOverrides(**recovery["adoption"]["parameters"])
            )
            recovery["lane_config"]["vllm_config"] = recovery["vllm_config"]
        provider_id, lane_id = recovery["provider_id"], recovery["lane_id"]
        self._require_lease()
        runtime = await self.registry.send_command(provider_id, "get_lanes", {}, timeout_seconds=30)
        lane = next((item for item in runtime.get("lanes", []) if item.get("lane_id") == lane_id), None)
        budget = {"nightly_gpu_budget": True} if recovery.get("gpu_budget") else {}
        if not force_restart and (budget or recovery.get("adoption")) and self._restored_lane_is_ready(lane, recovery):
            return
        self._require_lease()
        replies = []
        if lane is None:
            if budget and not recovery.get("adoption") and recovery.get("required_mib_by_gpu"):
                budget["nightly_restore_mib_by_gpu"] = recovery["required_mib_by_gpu"]
            result = await self.registry.send_command(
                provider_id,
                "add_lane",
                {**recovery["lane_config"], **budget},
                timeout_seconds=900,
                on_dispatched=replies.append,
            )
        else:
            result = await self.registry.send_command(
                provider_id,
                "reconfigure_lane",
                {
                    "lane_id": lane_id,
                    "updates": {"vllm_config": recovery["vllm_config"]},
                    "require_idle": False,
                    "force_restart": True,
                    **budget,
                },
                timeout_seconds=900,
                on_dispatched=replies.append,
            )
        applied = (result.get("lane_config") or {}).get("vllm_config")
        if applied != recovery["vllm_config"] or (lane is not None and not result.get("restarted")):
            raise RuntimeError("Worker did not confirm the production configuration")

        # The command can return before the routing snapshot catches up with
        # the restart. Wait for that lane instead of asking the planner to load it again.
        await self._wait_for_lane_status(provider_id, lane_id, replies[0].result(), recovery)

    @staticmethod
    def _require_lease():
        if not LEASE_CHECK.get()():
            raise NightlyStopped("lease_lost")

    async def _remove_lane(self, provider_id, lane_id, *, expected_model=None):
        self._require_lease()
        replies = []
        params = {"lane_id": lane_id}
        if expected_model is not None:
            params["expected_model"] = expected_model
        try:
            await self.registry.send_command(
                provider_id, "delete_lane", params, timeout_seconds=900, on_dispatched=replies.append
            )
        except LogosNodeCommandError as exc:
            if expected_model is not None and exc.reason_code == "nightly_lane_identity":
                return False
            if not self._lane_not_found(exc, lane_id):
                raise
        await self._wait_for_lane_status(provider_id, lane_id, replies[0].result(), None)
        return True

    @staticmethod
    def _lane_not_found(error, lane_id):
        return str(error) == str(KeyError(f"Lane '{lane_id}' not found"))

    async def _add_removed_lane(self, provider_id, config):
        self._require_lease()
        lane_id = configured_lane_id(config)
        expectation = {"model": config["model"], "lane_config": config, "exact": True}
        replies = []
        try:
            # Dispatch even when the lane appears present. The worker serializes
            # mutations of this ID, including a deletion whose reply was lost.
            lane = await self.registry.send_command(
                provider_id, "add_lane", config, timeout_seconds=900, on_dispatched=replies.append
            )
        except LogosNodeCommandError:
            if not replies:
                raise
            runtime = await self.registry.send_command(provider_id, "get_lanes", {}, timeout_seconds=30)
            lane = next((item for item in runtime.get("lanes", []) if item.get("lane_id") == lane_id), None)
            if lane is None:
                raise
        if lane.get("model") != config["model"] or lane.get("lane_id") != lane_id:
            raise RuntimeError(f"Worker returned a different lane while restoring {lane_id}")
        actual = lane.get("lane_config")
        if actual is None:
            raise RuntimeError(f"Worker did not report the configuration for lane {lane_id}")
        if actual != config:
            self._require_lease()
            # A normal add applies node defaults. Restore any explicit live
            # overrides a previous operation had set on the original lane.
            await self.registry.send_command(
                provider_id,
                "reconfigure_lane",
                {"lane_id": lane_id, "updates": config, "require_idle": True, "force_restart": True},
                timeout_seconds=900,
                on_dispatched=replies.append,
            )
        await self._wait_for_lane_status(provider_id, lane_id, replies[-1].result(), expectation)
        self.registry.update_desired_lane_add(provider_id, config, lane_id=lane_id)

    async def _restore_slept_lane(self, provider_id, config):
        self._require_lease()
        lane_id = configured_lane_id(config)
        expectation = {"model": config["model"], "lane_config": config, "exact": True}
        replies = []
        try:
            # Wait behind a possibly in-flight sleep/wake without restarting it.
            lane = await self.registry.send_command(
                provider_id,
                "reconfigure_lane",
                {"lane_id": lane_id, "updates": {}, "require_idle": True},
                timeout_seconds=900,
                on_dispatched=replies.append,
            )
        except LogosNodeCommandError as exc:
            if not self._lane_not_found(exc, lane_id):
                raise
            await self._add_removed_lane(provider_id, config)
            return
        if lane.get("lane_config") != config:
            raise RuntimeError(f"Static lane {lane_id} no longer has its original configuration")
        if lane.get("sleep_state") == "sleeping":
            self._require_lease()
            await self.registry.send_command(
                provider_id, "wake_lane", {"lane_id": lane_id}, timeout_seconds=900, on_dispatched=replies.append
            )
        await self._wait_for_lane_status(provider_id, lane_id, replies[-1].result(), expectation)

    async def _wait_for_lane_status(self, provider_id, lane_id, answer, recovery):
        state = "awaiting a fresh status"
        try:
            async with asyncio.timeout(LANE_READY_STATUS_TIMEOUT_SECONDS):
                while True:
                    if not LEASE_CHECK.get()() or not self.registry.is_provider_online(provider_id):
                        raise RuntimeError(f"Lost the worker or maintenance lock while restoring lane {lane_id}")
                    if self.registry.reflects_command(provider_id, answer):
                        snapshot = self.registry.peek_runtime_snapshot(provider_id) or {}
                        lanes = (snapshot.get("runtime") or {}).get("lanes") or []
                        lane = next((item for item in lanes if item.get("lane_id") == lane_id), None)
                        if recovery is None:
                            confirmed = lane is None
                        elif recovery.get("sleeping"):
                            confirmed = (
                                lane is not None
                                and lane.get("sleep_state") == "sleeping"
                                and lane.get("transition") is None
                            )
                        else:
                            confirmed = self._restored_lane_is_ready(lane, recovery)
                        if confirmed:
                            return
                        state = (
                            "lane missing"
                            if lane is None
                            else (
                                f"runtime={lane.get('runtime_state')}, sleep={lane.get('sleep_state')}, "
                                f"transition={lane.get('transition')}, active_requests={lane.get('active_requests')}"
                            )
                        )
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            expected = (
                "absent"
                if recovery is None
                else "sleeping" if recovery.get("sleeping") else "ready with those settings"
            )
            raise RuntimeError(
                f"Worker {provider_id} did not report that lane {lane_id} {expected} "
                f"within {LANE_READY_STATUS_TIMEOUT_SECONDS:g}s ({state})."
            ) from exc

    @staticmethod
    def _restored_lane_is_ready(lane, recovery):
        return (
            lane is not None
            and lane.get("model") == recovery["model"]
            and (
                lane.get("lane_config") == recovery["lane_config"]
                if recovery.get("exact")
                else (lane.get("lane_config") or {}).get("vllm_config") == recovery["vllm_config"]
            )
            and lane_is_serving(lane)
            and lane.get("sleep_state") in {"awake", "unsupported"}
            and lane.get("transition") is None
            and lane.get("active_requests") == 0
            and not any((lane.get("backend_metrics") or {}).get(key) for key in ("requests_running", "queue_waiting"))
        )

    async def settle(self, pair_id, recovery, report):
        """Complete a journaled adoption, or restore the original settings on failure."""
        adoption = recovery.get("adoption")
        if adoption:
            report[:] = [item for item in report if item.get("status") != "adopted"]
        if adoption and not store.status(pair_id)["enabled"]:
            recovery.pop("adoption")
            store.save(pair_id, "running", report, recovery)
            adoption = None
        try:
            await self.restore(pair_id, recovery)
            if not LEASE_CHECK.get()():
                raise RuntimeError("Maintenance lock lost; worker recovery remains pending")
            if adoption and not store.status(pair_id)["enabled"]:
                raise RuntimeError("Nightly schedule disabled during adoption")
        except Exception as exc:
            if not adoption or not LEASE_CHECK.get()():
                raise
            recovery.pop("adoption")
            failure = {**adoption, "status": "adoption_failed", "reason": str(exc)[:1000]}
            report.append(failure)
            # Persist rollback intent and the admin alert before sending another worker command.
            store.save(pair_id, "running", report, recovery, adoption_failure=failure)
            await self.restore(pair_id, recovery)
            if not LEASE_CHECK.get()():
                raise RuntimeError("Maintenance lock lost; worker recovery remains pending")
        else:
            if adoption:
                report.append({"status": "adopted", **adoption})

    def target_lane(self, pair_id, *, disable_missing=True):
        """Resolve and validate a currently usable model/worker pair."""
        with DBManager() as db:
            target = db.get_model_provider_benchmark_target(pair_id)
        if not target:
            if disable_missing:
                store.disable_missing_target(pair_id)
            raise RuntimeError("The scheduled model/worker deployment no longer exists")
        if target["provider_type"] != "logosnode":
            raise RuntimeError("Nightly benchmarks require a Logos worker")
        pid, model = int(target["provider_id"]), str(target["model_name"])
        if not self.healthy(pid):
            raise RuntimeError("The selected worker must be online and healthy")
        if self.planner.is_worker_locked(pid):
            raise RuntimeError("The selected worker is locked")
        snapshot = self.registry.peek_runtime_snapshot(pid)
        if not (snapshot.get("runtime") or {}).get("supports_nightly_gpu_budget"):
            raise RuntimeError("Update this worker to support Nightly GPU budgets")
        lane = self._select_lane(pid, model, snapshot)
        if lane is not None:
            try:
                limits = worker_limits(snapshot, model, lane=lane)
                baseline = parameter_plan(limits["current"])[0]
                validate_worker_overrides(baseline.serving_overrides, limits)
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
        return target, snapshot, lane

    def _select_lane(self, provider_id, model, snapshot):
        lanes = snapshot["runtime"].get("lanes", [])
        gpu_groups = {}
        for lane in lanes:
            if lane.get("runtime_state") in {"starting", "loaded", "running", "sleeping"}:
                selector = lane.get("occupied_gpu_devices") or (lane.get("lane_config") or {}).get("gpu_devices") or ""
                gpu_groups[lane["lane_id"]] = {int(gpu) for gpu in selector.split(",") if gpu.strip().isdigit()}

        def shares_gpu(lane_id):
            group = gpu_groups.get(lane_id, set())
            return any(group & other_group for other_id, other_group in gpu_groups.items() if other_id != lane_id)

        candidates = [
            lane
            for lane in lanes
            if lane.get("model") == model
            and lane_is_serving(lane)
            and (lane.get("lane_config") or {}).get("vllm")
            and not shares_gpu(lane["lane_id"])
        ]
        # A warm lane lock protects against idle eviction. Scheduled maintenance
        # may reuse the lane when no unlocked replica is available.
        return min(
            candidates,
            key=lambda lane: (
                self.planner.is_lane_locked(provider_id, lane["lane_id"]),
                not lane.get("is_static"),
                lane["lane_id"],
            ),
            default=None,
        )

    def _temporary_config(self, pair_id, provider_id, model, lanes):
        ids = {lane["lane_id"] for lane in lanes}
        lane_id = f"nightly-{pair_id}"
        while lane_id in ids:
            lane_id += "-temp"
        saved = next(
            (
                lane["lane_config"]
                for lane in lanes
                if lane.get("model") == model and (lane.get("lane_config") or {}).get("vllm")
            ),
            None,
        )
        if saved:
            # The worker assigns a port to the temporary identifier.
            return {**copy.deepcopy(saved), "lane_id": lane_id, "gpu_devices": ""}
        profile = self.planner._safe_get_profiles(provider_id).get(model)
        if profile and profile.engine != "vllm":
            raise RuntimeError("Nightly tuning requires a vLLM model")
        params = self.planner._build_load_params(
            model, lane_id, profile, self.planner._safe_get_capacity(provider_id), provider_id
        )
        return {**params, "vllm": True}

    def preview_configuration(self, pair_id, provider_id, model, snapshot):
        """Estimate the baseline settings before the worker confirms a ready lane."""
        runtime = (snapshot or {}).get("runtime") or {}
        if not runtime.get("supports_nightly_gpu_budget"):
            return None
        try:
            lane = self._select_lane(provider_id, model, snapshot)
            if lane is None:
                config = self._temporary_config(pair_id, provider_id, model, runtime.get("lanes") or [])
                lane = {"lane_config": config}
            return lane_serving_configuration(lane)
        except RuntimeError:
            return None

    async def _create_temporary_lane(self, recovery):
        self._require_lease()
        provider_id, lane_id, model = recovery["provider_id"], recovery["lane_id"], recovery["model"]
        profile = self.planner._safe_get_profiles(provider_id).get(model)
        projected_ram = self.planner._estimate_lane_host_ram_mb(
            provider_id, lane_id, model, profile, runtime_state="cold"
        )
        if projected_ram > 0:
            ram_ok, _, _ = self.planner._check_host_ram_headroom_for_cold_load(provider_id, model, projected_ram)
            if not ram_ok:
                raise NightlyCapacityDeferred(f"Worker lacks host memory to safely load the benchmark model {model}")
        replies = []
        lane = await self.registry.send_command(
            provider_id,
            "add_lane",
            {**recovery["lane_config"], "nightly_gpu_budget": True},
            timeout_seconds=900,
            on_dispatched=replies.append,
        )
        config = lane.get("lane_config")
        if lane.get("lane_id") != lane_id or lane.get("model") != model or not (config or {}).get("vllm"):
            raise RuntimeError("Worker did not confirm the temporary benchmark lane")
        recovery.update(lane_config=config, vllm_config=config.get("vllm_config") or {})
        await self._wait_for_lane_status(provider_id, lane_id, replies[0].result(), recovery)

    async def recover(self, row, connection):
        """Fence an orphaned nightly execution, confirm drain, then restore its journal."""
        import importlib

        main = importlib.import_module("logos.main")

        pair_id = row["model_provider_id"]
        with store.execution_lock(pair_id) as execution:
            if execution is None:
                return False
            jobs = store.recovery_jobs(pair_id)
            orphan_jobs = list(jobs)
            for other in store.rows():
                other_id = other["model_provider_id"]
                if other_id == pair_id or not other["recovery"]:
                    continue
                # A crashed parallel wave can leave several running jobs.
                # Never treat a sibling as orphaned while its execution lease survives.
                with store.execution_lock(other_id) as sibling:
                    if sibling is None:
                        return False
                orphan_jobs.extend(store.recovery_jobs(other_id))
            # Durable manual claims and unrelated jobs always retain their slot.
            if store.has_active_benchmarks(excluding=orphan_jobs):
                return False
            if any(not task.done() for task in main._benchmark_tasks_by_job.values()):
                return False

            def held():
                return store.lock_held(connection) and store.execution_lock_held(execution, pair_id)

            token = LEASE_CHECK.set(held)
            try:
                if not held():
                    return False
                store.fence_recovery_jobs(jobs)
                for job in jobs:
                    benchmark_reservations.release(job)
                # Cached telemetry cannot prove that requests from the old
                # execution have ended. Query the worker and fail closed.
                recovery = row["recovery"]
                if (jobs or recovery.get("mutated", True)) and not await self.recovery_worker_idle(recovery):
                    return False
                if not held():
                    return False
                store.finish_recovery_jobs(jobs)
                await self.settle(pair_id, recovery, row["report"])
                now = local_now()
                store.finish(
                    pair_id,
                    "interrupted_recovered",
                    row["report"],
                    recovery.get("search"),
                    resume_after_recovery=row.get("last_run_date") == now.date() and can_start(now),
                )
                benchmark_reservations.release(-pair_id)
                return True
            finally:
                LEASE_CHECK.reset(token)

    async def recovery_worker_idle(self, recovery):
        """True when the worker reports no request on the lane a recovery restores.

        Only the worker's own counters count. The orchestrator's in-flight
        counter can stay above zero after a request ended, and it cannot see
        requests from the crashed execution anyway.

        Check lanes of the benchmarked model and lanes the journal will
        restore. A legacy journal without lane and model requires the whole
        worker to be idle.
        """
        provider_id = recovery["provider_id"]
        if not self.healthy(provider_id):
            return False
        runtime = await self.registry.send_command(provider_id, "get_runtime", {}, timeout_seconds=30)
        lanes = runtime.get("lanes")
        if (runtime.get("node_health") or {}).get("healthy") is False:
            return False
        if not isinstance(lanes, list):
            return False
        lane_id, model = recovery.get("lane_id"), recovery.get("model")
        affected_ids = {configured_lane_id(config) for config in recovery.get("removed_lanes", [])}
        affected_ids.update(configured_lane_id(config) for config in recovery.get("slept_lanes", []))
        if lane_id:
            affected_ids.add(lane_id)
        if affected_ids or model:
            lanes = [
                lane for lane in lanes if lane.get("lane_id") in affected_ids or (model and lane.get("model") == model)
            ]
        for lane in lanes:
            if lane.get("active_requests") != 0:
                return False
            metrics = lane.get("backend_metrics") or {}
            if any(float(metrics.get(key) or 0) != 0 for key in ("requests_running", "queue_waiting")):
                return False
        return True

    async def run_schedule(self, pair_id, connection):
        with store.execution_lock(pair_id) as execution:
            if execution is None:
                return False
            token = LEASE_CHECK.set(
                lambda: store.lock_held(connection) and store.execution_lock_held(execution, pair_id)
            )
            try:
                return await self._run_schedule(pair_id, connection)
            finally:
                LEASE_CHECK.reset(token)

    async def _run_schedule(self, pair_id, connection):
        report = []
        recovery = None
        search = None
        can_adopt = False
        state = "completed"
        try:
            target, _, _ = self.target_lane(pair_id)
            pid, model = int(target["provider_id"]), str(target["model_name"])
            self._require_lease()
            self.planner._facade.reserve_model_for_drain(pid, model, -pair_id)
            runtime = await self.registry.send_command(pid, "get_runtime", {}, timeout_seconds=30)
            if not isinstance(runtime.get("lanes"), list) or (runtime.get("node_health") or {}).get("healthy") is False:
                raise RuntimeError("Could not confirm the worker's existing lanes")
            snapshot = {"runtime": runtime}
            lanes = runtime["lanes"]
            lane = self._select_lane(pid, model, snapshot)
            temporary = lane is None
            can_adopt = not temporary and sum(item.get("model") == model for item in lanes) == 1
            config = (
                self._temporary_config(pair_id, pid, model, lanes) if temporary else copy.deepcopy(lane["lane_config"])
            )
            recovery = {
                "provider_id": pid,
                "model": model,
                "lane_id": config["lane_id"] if temporary else lane["lane_id"],
                "vllm_config": config.get("vllm_config") or {},
                "lane_config": config,
                "model_scoped": True,
                "mutated": False,
                "benchmark_lane_mutated": False,
                **({"temporary_lane": True} if temporary else {}),
            }
            # Journal first; a process restart must not lose the original settings or lane layout.
            store.save(pair_id, "preparing", report, recovery)
            async with asyncio.timeout(900):
                while self.planner._facade.model_inflight_requests(pid, model) > 0:
                    self._require_lease()
                    await asyncio.sleep(1)
                # Fresh counters from the test model are sufficient. Co-resident
                # models keep serving, including on the same GPU.
                while not await self.recovery_worker_idle(recovery):
                    self._require_lease()
                    await asyncio.sleep(1)
            if any(
                item.get("transition") or item.get("runtime_state") == "starting"
                for item in lanes
                if item["model"] == model
            ):
                raise RuntimeError("Worker lanes are still changing; nightly benchmark will wait")
            self._require_lease()
            if not store.status(pair_id)["enabled"]:
                raise NightlyStopped("disabled")
            if not can_start(local_now()):
                raise NightlyStopped("window_closed")
            benchmark_reservations.pin_lane(pid, model, -pair_id, recovery["lane_id"])
            # The worker's budget check acquires a GPU hold. Journal its stable
            # lane key before dispatch, including when the reply gets lost.
            recovery["gpu_budget"] = True
            recovery["mutated"] = temporary
            store.save(pair_id, "preparing", report, recovery)
            if temporary:
                await self._create_temporary_lane(recovery)
                snapshot = self.registry.peek_runtime_snapshot(pid) or {}
                lane = next(
                    (
                        item
                        for item in (snapshot.get("runtime") or {}).get("lanes", [])
                        if item.get("lane_id") == recovery["lane_id"]
                    ),
                    None,
                )
                if lane is None:
                    raise RuntimeError("Worker no longer reports the temporary benchmark lane")
            else:
                assessment = await self.registry.send_command(
                    pid, "check_nightly_gpu_budget", {"lane_id": recovery["lane_id"]}, timeout_seconds=30
                )
                required_mib = assessment.get("required_mib_by_gpu")
                if not isinstance(required_mib, dict) or not required_mib:
                    raise RuntimeError("Worker did not report the original lane's GPU budget")
                recovery["required_mib_by_gpu"] = required_mib
                store.save(pair_id, "preparing", report, recovery)
            current = lane_serving_configuration(lane)
            limits = worker_limits(snapshot, model, lane=lane)
            plan = parameter_plan(current)
            schedule = store.status(pair_id)
            if recent_requests.username() or schedule.get("workload_id"):
                workload = (
                    recent_requests.for_model(target, local_now())
                    if recent_requests.username()
                    else workloads.load(schedule["workload_id"], target["model_id"])
                )
                if recent_requests.username():
                    store.bind_workload(pair_id, workload["id"])
                search = optimization.context(current, snapshot, workload, schedule.get("optimizer") or {})
                recovery["search"] = search
                plan = optimization.plan(
                    current, search, round_index=optimization.round_index(search["state"], local_now().date())
                )
            for index, settings in enumerate(plan):
                if self.calibration and self.calibration.benchmarks_must_wait():
                    state = "calibration_pending"
                    break
                for attempt in range(2):
                    if self.calibration and self.calibration.benchmarks_must_wait():
                        state = "calibration_pending"
                        break
                    if not can_start(local_now()) or not store.status(pair_id)["enabled"]:
                        state = "window_closed" if store.status(pair_id)["enabled"] else "disabled"
                        raise NightlyStopped(state)
                    if not self.healthy(pid):
                        raise RuntimeError("Worker is no longer healthy")
                    if not LEASE_CHECK.get()():
                        raise NightlyStopped("lease_lost")
                    try:
                        validate_worker_overrides(settings.serving_overrides, limits)
                        recovery["mutated"] = True
                        recovery["benchmark_lane_mutated"] = True
                        store.save(pair_id, "running", report, recovery)
                        job_id, task = await self.submit(pair_id, settings)
                        result = await task
                        if result:
                            if search and index == 0 and search["reference_id"] is None:
                                search["reference_id"] = result
                            baseline_id = (
                                result
                                if index == 0
                                else next(
                                    (
                                        item["benchmark_id"]
                                        for item in report
                                        if item.get("configuration") == 1 and item.get("status") == "success"
                                    ),
                                    None,
                                )
                            )
                            comparison_args = (
                                result,
                                baseline_id,
                                settings.serving_overrides.model_dump(exclude_none=True),
                                current,
                            )
                            if search:
                                store.record_comparison(*comparison_args, search=search)
                            else:
                                store.record_comparison(*comparison_args)
                        outcome = {
                            "configuration": index + 1,
                            "attempt": attempt + 1,
                            "job_id": job_id,
                            "benchmark_id": result,
                            "status": "success" if result else "failed",
                            "parameters": settings.serving_overrides.model_dump(exclude_none=True),
                        }
                        report.append(outcome)
                        if search and can_adopt:
                            adoption = store.adoption_candidate(pair_id, search, report)
                            if adoption:
                                validate_worker_overrides(ServingOverrides(**adoption["parameters"]), limits)
                                recovery["adoption"] = adoption
                        store.save(pair_id, "running", report, recovery)
                        break
                    except NightlyCapacityDeferred:
                        raise
                    except NightlyStopped as exc:
                        report.append({"configuration": index + 1, "attempt": attempt + 1, "status": exc.reason})
                        store.save(pair_id, "running", report, recovery)
                        if exc.reason != "stalled":
                            state = exc.reason
                            raise
                        # Reset the lane before one bounded retry, even with identical settings.
                        await self._restore_lane(recovery, force_restart=True)
                    except (ValueError, RuntimeError) as exc:
                        report.append({"configuration": index + 1, "status": "failed", "reason": str(exc)[:1000]})
                        store.save(pair_id, "running", report, recovery)
                        break
                if index == 0 and not any(
                    item.get("configuration") == 1 and item.get("status") == "success" for item in report
                ):
                    if state == "completed":
                        state = "failed"
                    break
        except NightlyCapacityDeferred as exc:
            state = CAPACITY_WAITING
            report.append({"status": state, "reason": exc.detail[:1000]})
        except LogosNodeCommandError as exc:
            state = CAPACITY_WAITING if exc.reason_code == NIGHTLY_GPU_BUDGET_REASON else "failed"
            report.append({"status": state, "reason": str(exc)[:1000]})
        except NightlyStopped as exc:
            state = exc.reason
        except asyncio.CancelledError:
            state = "cancelled"
            if asyncio.current_task().cancelling():
                state = "interrupted"
                raise
        except Exception as exc:
            state = "failed"
            report.append({"status": "failed", "reason": str(exc)[:1000]})
        finally:
            settled = recovery is None
            if recovery:
                try:
                    if not LEASE_CHECK.get()():
                        raise RuntimeError("Maintenance lock lost; worker recovery remains pending")
                    if state == CAPACITY_WAITING and recovery.pop("adoption", None):
                        store.save(pair_id, "running", report, recovery)
                    if (
                        search
                        and can_adopt
                        and state in {"completed", "window_closed"}
                        and store.status(pair_id)["enabled"]
                    ):
                        adoption = store.adoption_candidate(pair_id, search, report)
                        if adoption:
                            validate_worker_overrides(ServingOverrides(**adoption["parameters"]), limits)
                            recovery["adoption"] = adoption
                            store.save(pair_id, "running", report, recovery)
                    await self.settle(pair_id, recovery, report)
                    settled = True
                except Exception as exc:
                    state = "recovery_required"
                    report.append({"status": state, "reason": str(exc)[:1000]})
            if settled:
                baseline_ok = any(item.get("configuration") == 1 and item.get("status") == "success" for item in report)
                successful = sum(item.get("status") == "success" for item in report)
                if (
                    state == "completed"
                    and baseline_ok
                    and successful > 1
                    and can_start(local_now())
                    and store.status(pair_id)["enabled"]
                ):
                    state = "continuing"
                try:
                    store.finish(pair_id, state, report, search)
                except Exception:
                    # The worker remains reserved until its configuration and result are durable together.
                    if recovery:
                        store.save(pair_id, "recovery_required", report, recovery)
                    raise
                recovery = None
                benchmark_reservations.release(-pair_id)
            else:
                store.save(pair_id, state, report, recovery)
        return recovery is None

    async def submit(self, pair_id, settings):
        # Reuse the authenticated admission path and job/result persistence.
        import importlib

        main = importlib.import_module("logos.main")
        from logos.routers.internal import internal_run_model_benchmark

        request = Request(
            {"type": "http", "headers": [(b"authorization", ("Bearer " + main._INTERNAL_SECRET).encode())]}
        )
        token = NIGHTLY_CONTEXT.set(True)
        try:
            response = await internal_run_model_benchmark(
                InternalBenchmarkRequest(model_provider_id=pair_id, **settings.model_dump()), request
            )
            payload = json.loads(response.body)
            if response.status_code != 202:
                raise RuntimeError(payload.get("error", "Benchmark admission failed"))
            job_id = payload["job_id"]
            return job_id, main._benchmark_tasks_by_job[job_id]
        finally:
            NIGHTLY_CONTEXT.reset(token)
