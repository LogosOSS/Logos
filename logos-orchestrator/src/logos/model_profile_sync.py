"""Keeps the orchestrator's model profiles and a worker's statuses in step.

One instance per process (see ``profile_sync``). It records every worker
status, writes the changed profiles to the database, offers fresh
calibrations to identical nodes and pushes stored profiles back to workers.
The state below assumes a single orchestrator process.
"""

import asyncio
import datetime
import logging
import threading
import time
from typing import Any, Dict

import logos.main as _main
from logos.dbutils.dbmanager import DBManager
from logos.logosnode_snapshot import _build_live_local_provider_sample, _parse_iso_datetime
from logos.main import _resolve_provider_name
from logos.model_profile_store import (
    SYNC_MODEL_PROFILES_ACTION,
    ProfileWriteCache,
    calibration_snapshot,
    effective_profile,
    is_central_profile_payload,
    is_new_local_calibration,
    material_profile,
    overridden_fields,
    persistable_profile,
    profile_digest,
    reported_profile,
    shared_profiles_enabled,
)

logger = logging.getLogger("LogosLogger")

# Lets a push that is already in flight land before a rejected echo re-sends.
_RESYNC_DELAY_SECONDS = 5.0
# A worker that keeps rejecting pushes (its revision ahead of a restored
# database, a model it is calibrating) is re-sent ever more rarely.
_RESYNC_MAX_DELAY_SECONDS = 300.0
# A request count change resends the last status with new counters only,
# twice per request; the database keeps at most one of those per interval.
_COUNT_ONLY_SNAPSHOT_SECONDS = 5.0


def _with_cold_marks(provider_id: int, runtime: Dict[str, Any]) -> Dict[str, Any]:
    """The worker's runtime with the planner's cold mark added to each lane.

    The mark lives only in this orchestrator and decides whether the scheduler
    routes to a lane at all, so the lane health panel has to show it next to
    what the worker reports.
    """
    registry = _main._logosnode_registry
    lanes = runtime.get("lanes")
    if registry is None or not isinstance(lanes, list):
        return runtime
    marked = [
        (
            {**lane, "cold_marked": registry.is_lane_cold_marked(provider_id, str(lane.get("lane_id") or ""))}
            if isinstance(lane, dict)
            else lane
        )
        for lane in lanes
    ]
    return {**runtime, "lanes": marked}


class ModelProfileSyncService:
    def __init__(self) -> None:
        self.write_cache = ProfileWriteCache()
        # Held while a calibration is reserved and while an adoption checks for
        # one and commits, so an adoption either lands before the reservation
        # answers or sees it.
        self.adoption_gate = threading.Lock()
        # (provider_id, model) -> last_measured_epoch already snapshotted
        self.recorded_calibrations: dict[tuple[int, str], float] = {}
        # provider_id -> models whose echo was rejected, awaiting one coalesced push
        self.pending_resyncs: dict[int, set[str]] = {}
        # provider_id -> resyncs since its echoes were last accepted
        self.resync_rounds: dict[int, int] = {}
        # provider_id -> newest status not yet written (sample, count-only flag)
        self.unsaved_samples: dict[int, tuple[Dict[str, Any], bool]] = {}
        # provider_id -> the task writing its statuses, one at a time
        self.sample_writers: dict[int, asyncio.Task] = {}
        # providers already warned about a worker that predates central profiles
        self.outdated_format_warned: set[int] = set()
        # provider_id -> runtime timestamp of its previous status
        self.last_runtime_ts: dict[int, Any] = {}
        # provider_id -> monotonic time its last snapshot write started
        self.last_snapshot_at: dict[int, float] = {}

    async def capture_status(
        self,
        provider_id: int,
        runtime: Dict[str, Any],
        *,
        cold_mark_changed: bool = False,
    ) -> None:
        """Record a worker status and queue its database write.

        ``cold_mark_changed`` re-captures the worker's last status because the
        planner's cold mark on one of its lanes changed. It is always written,
        under a fresh timestamp so it sorts after the status it repeats, and it
        counts as a repeat of that status, so profiles are not stored again.
        """
        heartbeat = datetime.datetime.now(datetime.timezone.utc).isoformat() if cold_mark_changed else None
        sample = _build_live_local_provider_sample(
            None,
            {
                "last_heartbeat": heartbeat or runtime.get("timestamp"),
                "runtime": _with_cold_marks(provider_id, runtime),
            },
        )
        if sample is None:
            return
        # Scheduler signals read every status from memory; only the database
        # write below is coalesced and runs off the receive loop.
        await _main._logosnode_registry.record_runtime_sample(provider_id, sample)
        runtime_ts = runtime.get("timestamp")
        count_only = cold_mark_changed or (
            runtime_ts is not None and self.last_runtime_ts.get(provider_id) == runtime_ts
        )
        self.last_runtime_ts[provider_id] = runtime_ts
        last_write = self.last_snapshot_at.get(provider_id)
        if (
            count_only
            and not cold_mark_changed
            and last_write is not None
            and time.monotonic() - last_write < _COUNT_ONLY_SNAPSHOT_SECONDS
        ):
            return
        pending = self.unsaved_samples.get(provider_id)
        # A status replaced before its write still owes its profiles.
        self.unsaved_samples[provider_id] = (sample, count_only and (pending is None or pending[1]))
        if provider_id in self.sample_writers:
            return
        task = asyncio.create_task(self._write_status_samples(provider_id))
        self.sample_writers[provider_id] = task
        _main._background_tasks.add(task)
        task.add_done_callback(_main._background_tasks.discard)

    async def _write_status_samples(self, provider_id: int) -> None:
        """Write a worker's statuses one at a time, newest first, older dropped."""
        try:
            while (entry := self.unsaved_samples.pop(provider_id, None)) is not None:
                sample, count_only = entry
                self.last_snapshot_at[provider_id] = time.monotonic()
                try:
                    persisted = await asyncio.to_thread(self._persist_status, provider_id, sample, count_only)
                except Exception:
                    # A malformed sample must not end the writer for later ones.
                    logger.warning("Dropping an unwritable status of provider %s", provider_id, exc_info=True)
                    continue
                if persisted is None:
                    continue
                changed_models, rejected_models = persisted
                if changed_models:
                    self._snapshots_linked(provider_id, changed_models)
                    self.schedule_push(provider_id, changed_models)
                    await self._offer_to_siblings(provider_id, changed_models)
                if rejected_models:
                    self._schedule_resync(provider_id, rejected_models)
                elif not count_only:
                    self.resync_rounds.pop(provider_id, None)
        finally:
            self.sample_writers.pop(provider_id, None)

    async def handle_calibration_rejected(self, provider_id: int, event: Dict[str, Any]) -> None:
        """Drop a shared calibration a node could not start its lane with."""
        model_name = str(event.get("model") or "")
        try:
            calibration_id = int(str(event.get("details") or "").strip())
        except ValueError:
            return

        def _reject() -> list[tuple[int, str]]:
            with DBManager() as db:
                affected = db.reject_shared_calibration(provider_id, model_name, calibration_id)
                db.session.commit()
                return affected

        try:
            affected = await asyncio.to_thread(_reject)
        except Exception:
            logger.warning("Failed to reject calibration %s of provider %s", calibration_id, provider_id, exc_info=True)
            return
        if affected:
            logger.warning(
                "calibration_rejected provider=%s model=%s calibration_id=%s dropped_on=%s",
                _resolve_provider_name(provider_id),
                model_name,
                calibration_id,
                sorted({pid for pid, _ in affected}),
            )
        by_provider: Dict[int, list[str]] = {}
        for affected_provider, affected_model in affected:
            self.write_cache.forget(affected_provider, affected_model)
            by_provider.setdefault(affected_provider, []).append(affected_model)
        for affected_provider, names in by_provider.items():
            self.schedule_push(affected_provider, names)

    async def _offer_to_siblings(self, provider_id: int, model_names: list[str]) -> None:
        try:
            adopters = await asyncio.to_thread(self.offer_to_siblings_now, provider_id, model_names)
        except Exception:
            logger.warning("Failed to share calibrations of provider %s", provider_id, exc_info=True)
            return
        for sibling, names in adopters.items():
            self.schedule_push(sibling, names)

    def _persist_status(
        self, provider_id: int, sample: Dict[str, Any], count_only: bool = False
    ) -> tuple[list[str], list[str]] | None:
        """Store one status sample; None when even the snapshot failed.

        Returns, as ``self.persist_model_profiles``, the models the worker must be
        sent. A count-only status repeats profiles that are already stored.
        """
        timestamp = _parse_iso_datetime(sample.get("timestamp"))
        used_bytes = int(float(sample.get("used_vram_mb") or 0.0) * 1024 * 1024)
        total_vram_mb = sample.get("total_vram_mb")
        total_bytes = None
        if total_vram_mb is not None:
            total_bytes = int(float(total_vram_mb or 0.0) * 1024 * 1024)
        free_vram_mb = sample.get("remaining_vram_mb")
        free_bytes = None
        if free_vram_mb is not None:
            free_bytes = int(float(free_vram_mb or 0.0) * 1024 * 1024)
        runtime_payload = sample.get("runtime_payload") if isinstance(sample.get("runtime_payload"), dict) else {}
        # Profiles live in model_profiles; copying them into every snapshot only
        # grew a column nothing reads.
        snapshot_payload = {key: value for key, value in runtime_payload.items() if key != "model_profiles"}

        changed_models: list[str] = []
        rejected_models: list[str] = []
        try:
            with DBManager() as db:
                db.insert_provider_snapshot(
                    provider_id=provider_id,
                    snapshot_ts=timestamp,
                    total_models_loaded=int(sample.get("models_loaded") or 0),
                    total_vram_used_bytes=used_bytes,
                    total_memory_bytes=total_bytes,
                    free_memory_bytes=free_bytes,
                    loaded_models=list(sample.get("loaded_models") or []),
                    snapshot_source=str(sample.get("snapshot_source") or "logosnode-runtime"),
                    runtime_payload=snapshot_payload,
                    scheduler_signals=(
                        sample.get("scheduler_signals") if isinstance(sample.get("scheduler_signals"), dict) else {}
                    ),
                    poll_success=True,
                )
                model_profiles = runtime_payload.get("model_profiles")
                if not count_only and isinstance(model_profiles, dict) and model_profiles:
                    try:
                        changed_models, rejected_models = self.persist_model_profiles(db, provider_id, model_profiles)
                    except Exception:
                        db.session.rollback()
                        logger.warning(
                            "Failed to upsert model profiles for provider %s, the "
                            "entire row update (base_residency_mb, loaded_vram_mb, "
                            "kv_budget_mb, measurement_count, last_measured_at) is "
                            "lost until this recovers",
                            _resolve_provider_name(provider_id),
                            exc_info=True,
                        )
        except Exception:
            # Persisting a VRAM snapshot must never drop the worker's live session.
            # A missing table (the webservice migration that renames it has not run
            # yet) or a transient database error is logged and the next status
            # message retries; the worker stays connected and the capacity planner
            # degrades to its last known snapshot instead of going dark.
            logger.warning(
                "Failed to persist provider snapshot for %s; worker session stays "
                "connected and the next status message retries",
                _resolve_provider_name(provider_id),
                exc_info=True,
            )
            return None
        return changed_models, rejected_models

    def persist_model_profiles(
        self, db: DBManager, provider_id: int, model_profiles: Dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """Store a worker's echoed profiles in one transaction.

        Returns the models with a new central revision, and the models whose
        echo was rejected as outdated; the worker must be sent both.
        """
        if not is_central_profile_payload(model_profiles):
            if provider_id not in self.outdated_format_warned:
                self.outdated_format_warned.add(provider_id)
                logger.warning(
                    "Provider %s reports profiles in the pre-central format and is ignored; update its worker",
                    _resolve_provider_name(provider_id),
                )
            return [], []
        now = time.monotonic()
        changed: list[str] = []
        rejected: list[str] = []
        written: dict[str, tuple[str, str, tuple[int, int]]] = {}
        recorded: dict[str, float] = {}
        for model_name, echoed in model_profiles.items():
            if not isinstance(echoed, dict):
                continue
            try:
                revision = int(echoed.get("sync_revision") or 0)
            except (TypeError, ValueError):
                continue
            stored = persistable_profile(echoed)
            key_hash = echoed.get("calibration_key_hash") or None
            digest = profile_digest(revision, echoed)
            material = profile_digest(revision, material_profile(echoed))
            if not self.write_cache.unchanged(provider_id, model_name, digest, material, now):
                generation = self.write_cache.generation(provider_id, model_name)
                if not db.persist_central_model_profile(
                    provider_id,
                    model_name,
                    stored,
                    reported_profile(echoed),
                    revision,
                    key_hash,
                    overridden_fields(echoed),
                ):
                    rejected.append(model_name)
                    continue
                written[model_name] = (digest, material, generation)
            if not is_new_local_calibration(echoed):
                continue
            epoch = float(echoed["last_measured_epoch"])
            if self.recorded_calibrations.get((provider_id, model_name)) == epoch:
                continue
            new_revision = db.record_model_calibration(
                provider_id,
                model_name,
                calibration_snapshot(stored),
                echoed.get("calibration_key") if isinstance(echoed.get("calibration_key"), dict) else None,
                key_hash,
                datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc),
                echoed.get("calibration_duration_s"),
            )
            recorded[model_name] = epoch
            if new_revision is not None:
                changed.append(model_name)
        db.session.commit()
        # Only now: a rolled-back write must not count as stored, and one that
        # a reset or invalidation overtook since must not mask that change.
        for model_name, (digest, material, generation) in written.items():
            self.write_cache.remember(provider_id, model_name, digest, material, now, generation)
        for model_name in changed:
            self.write_cache.forget(provider_id, model_name)
        for model_name, epoch in recorded.items():
            self.recorded_calibrations[(provider_id, model_name)] = epoch
        return changed, rejected

    def load_effective_profiles(
        self, provider_id: int, model_names: list[str] | None = None
    ) -> Dict[str, Dict[str, Any]]:
        with DBManager() as db:
            rows = db.get_central_model_profiles(provider_id, model_names)
        return {str(row["model_name"]): effective_profile(provider_id, row) for row in rows}

    async def push_profiles(
        self,
        provider_id: int,
        model_names: list[str] | None = None,
        calibration_key_hashes: Dict[str, str] | None = None,
        slice_gpu_counts: Dict[str, int] | None = None,
    ) -> None:
        """Send the stored profiles to a worker that syncs with the database."""
        if not _main._logosnode_registry.supports_action(provider_id, SYNC_MODEL_PROFILES_ACTION):
            return
        try:
            if calibration_key_hashes:
                await asyncio.to_thread(
                    self.update_reported_keys, provider_id, calibration_key_hashes, slice_gpu_counts
                )
            profiles = await asyncio.to_thread(self.load_effective_profiles, provider_id, model_names)
            await _main._logosnode_registry.send_command(
                provider_id,
                SYNC_MODEL_PROFILES_ACTION,
                params={"profiles": profiles},
                timeout_seconds=30,
            )
        except Exception:
            # The worker keeps its current profiles; the next hello pushes again.
            logger.warning(
                "Failed to push model profiles to provider %s",
                _resolve_provider_name(provider_id),
                exc_info=True,
            )

    def update_reported_keys(
        self,
        provider_id: int,
        calibration_key_hashes: Dict[str, str],
        slice_gpu_counts: Dict[str, int] | None = None,
    ) -> None:
        """Store what a node reports about its models, then let it adopt."""
        with DBManager() as db:
            db.update_reported_calibration_keys(provider_id, calibration_key_hashes, slice_gpu_counts)
            db.session.commit()
            try:
                with self.adoption_gate:
                    # Its own result for the model in progress would be discarded.
                    busy = self.models_in_calibration(provider_id)
                    models = [name for name in calibration_key_hashes if name not in busy]
                    # Switched off, a calibration that stopped fitting still goes.
                    adopted = db.adopt_shared_calibrations(provider_id, models, adopt=shared_profiles_enabled())
                    db.session.commit()
            except Exception:
                # Sharing is an optimisation; it must not keep a worker from
                # starting or from receiving its profiles.
                db.session.rollback()
                logger.warning("Calibration sharing failed for provider %s", provider_id, exc_info=True)
                return
        for model_name, calibration_id in adopted:
            self.write_cache.forget(provider_id, model_name)
            logger.info(
                "calibration_adopted provider=%s model=%s calibration_id=%s at=%s",
                _resolve_provider_name(provider_id),
                model_name,
                calibration_id,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )

    def models_in_calibration(self, provider_id: int) -> frozenset[str]:
        lookup = getattr(_main._logosnode_registry, "models_in_calibration", None)
        return lookup(provider_id) if lookup is not None else frozenset()

    def _snapshots_linked(self, provider_id: int, model_names: list[str]) -> None:
        lift = getattr(_main._logosnode_registry, "snapshots_linked", None)
        if lift is not None:
            lift(provider_id, model_names)

    def offer_to_siblings_now(self, provider_id: int, model_names: list[str]) -> Dict[int, list[str]]:
        """Let every other node of the same key adopt what this node just measured."""
        if not shared_profiles_enabled():
            return {}
        adopters: Dict[int, list[str]] = {}
        adopted: list[tuple[int, str, int]] = []
        with self.adoption_gate, DBManager() as db:
            for model_name in model_names:
                for sibling, calibration_id in db.adopt_shared_calibration_for_siblings(
                    provider_id,
                    model_name,
                    skip=lambda sibling, model_name=model_name: model_name in self.models_in_calibration(sibling),
                ):
                    adopters.setdefault(sibling, []).append(model_name)
                    if calibration_id is not None:
                        adopted.append((sibling, model_name, calibration_id))
                db.session.commit()
        for sibling, names in adopters.items():
            for model_name in names:
                self.write_cache.forget(sibling, model_name)
        for sibling, model_name, calibration_id in adopted:
            logger.info(
                "calibration_adopted provider=%s model=%s calibration_id=%s at=%s",
                _resolve_provider_name(sibling),
                model_name,
                calibration_id,
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
        return adopters

    def schedule_push(
        self,
        provider_id: int,
        model_names: list[str] | None = None,
        calibration_key_hashes: Dict[str, str] | None = None,
        slice_gpu_counts: Dict[str, int] | None = None,
    ) -> None:
        task = asyncio.create_task(
            self.push_profiles(provider_id, model_names, calibration_key_hashes, slice_gpu_counts)
        )
        _main._background_tasks.add(task)
        task.add_done_callback(_main._background_tasks.discard)

    def _schedule_resync(self, provider_id: int, model_names: list[str]) -> None:
        """Re-send profiles a worker has not adopted, e.g. after a failed push.

        Every status repeats the rejected echo; all of them share one push, and
        each further round without an accepted echo waits twice as long.
        """
        pending = self.pending_resyncs.get(provider_id)
        if pending is not None:
            pending.update(model_names)
            return
        self.pending_resyncs[provider_id] = set(model_names)
        rounds = self.resync_rounds.get(provider_id, 0)
        self.resync_rounds[provider_id] = rounds + 1
        delay = min(_RESYNC_DELAY_SECONDS * 2 ** min(rounds, 16), _RESYNC_MAX_DELAY_SECONDS)
        task = asyncio.create_task(self._run_resync(provider_id, delay))
        _main._background_tasks.add(task)
        task.add_done_callback(_main._background_tasks.discard)

    async def _run_resync(self, provider_id: int, delay: float = _RESYNC_DELAY_SECONDS) -> None:
        try:
            await asyncio.sleep(delay)
        finally:
            model_names = self.pending_resyncs.pop(provider_id, set())
        if model_names:
            await self.push_profiles(provider_id, sorted(model_names))

    def forget_provider(self, provider_id: int) -> None:
        """Drop the bookkeeping of a node whose session ended.

        Not while a newer session of it runs; a writer still draining the last
        status removes itself.
        """
        self.last_runtime_ts.pop(provider_id, None)
        self.last_snapshot_at.pop(provider_id, None)
        self.resync_rounds.pop(provider_id, None)
        self.pending_resyncs.pop(provider_id, None)
        self.outdated_format_warned.discard(provider_id)
        for key in [key for key in self.recorded_calibrations if key[0] == provider_id]:
            del self.recorded_calibrations[key]
        self.write_cache.forget(provider_id)


profile_sync = ModelProfileSyncService()
