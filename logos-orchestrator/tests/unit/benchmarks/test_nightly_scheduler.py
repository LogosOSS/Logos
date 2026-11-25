import asyncio
import copy
from contextlib import nullcontext
from datetime import datetime, time, timedelta
from unittest.mock import DEFAULT, AsyncMock, MagicMock

import pytest

from logos.benchmarks import nightly_scheduler as module
from logos.benchmarks.nightly import (
    CAPACITY_RETRY_SECONDS,
    CAPACITY_WAITING,
    NIGHTLY_GPU_BUDGET_REASON,
    NightlyCapacityDeferred,
    NightlyStopped,
)
from logos.benchmarks.reservations import benchmark_reservations
from logos.maintenance_window import MaintenanceWindow

GPU_BUDGET = {"0": 12_288.0}


@pytest.fixture
def scheduler(monkeypatch):
    monkeypatch.delenv("LOGOS_MAINTENANCE_MAX_NODES", raising=False)
    registry = MagicMock()
    registry.is_provider_online.return_value = True
    registry.has_received_first_status.return_value = True
    registry.is_calibrating.return_value = False
    registry.is_cordoned.return_value = False

    def send_command(pid, action, *_args, **_kwargs):
        if action == "get_runtime" and benchmark_reservations.provider_owner(pid) in {-31, -32, -33}:
            return registry.peek_runtime_snapshot.return_value["runtime"]
        if action == "check_nightly_gpu_budget":
            return {"ok": True, "required_mib_by_gpu": GPU_BUDGET}
        return DEFAULT

    registry.send_command = AsyncMock(return_value={"lanes": []}, side_effect=send_command)
    registry.active_provider_ids.return_value = [20, 21]
    registry.peek_runtime_snapshot.return_value = {
        "runtime": {
            "supports_benchmark_restart": True,
            "supports_nightly_gpu_budget": True,
            "lanes": [
                {
                    "lane_id": "lane",
                    "runtime_state": "running",
                    "active_requests": 0,
                    "model": "model",
                    "lane_config": {
                        "lane_id": "lane",
                        "model": "model",
                        "vllm": True,
                        "vllm_config": {"max_num_seqs": 16, "max_num_batched_tokens": 4096},
                    },
                }
            ],
            "devices": {"nvidia_smi_available": True, "devices": [{"kind": "nvidia"}]},
        }
    }
    planner = MagicMock()
    planner._facade.model_inflight_requests.return_value = 0
    planner._drain_lane = AsyncMock(return_value=True)
    planner.is_worker_locked.return_value = False
    planner.is_lane_locked.return_value = False
    service = module.NightlyScheduler(registry, planner)
    service.restore = AsyncMock()
    db = MagicMock()
    db.__enter__.return_value = db
    db.get_model_provider_benchmark_target.return_value = {
        "provider_id": 20,
        "provider_type": "logosnode",
        "model_name": "model",
    }
    monkeypatch.setattr(module, "DBManager", lambda: db)
    monkeypatch.setattr(module, "local_now", lambda: datetime.fromisoformat("2026-10-13T05:00:00+02:00"))
    monkeypatch.setattr(module.store, "status", lambda _: {"enabled": True})
    monkeypatch.setattr(module.store, "lock_held", lambda _: True)
    monkeypatch.setattr(module.store, "execution_lock", lambda _: nullcontext(object()))
    monkeypatch.setattr(module.store, "execution_lock_held", lambda *args: True)
    monkeypatch.setattr(module.store, "recovery_jobs", lambda _: [])
    monkeypatch.setattr(module.store, "fence_recovery_jobs", lambda _: None)
    monkeypatch.setattr(module.store, "finish_recovery_jobs", lambda _: None)
    planner._facade.reserve_model_for_drain.side_effect = benchmark_reservations.reserve_nightly
    monkeypatch.setattr(module.store, "record_comparison", MagicMock())
    monkeypatch.setattr(module.store, "adoption_candidate", MagicMock(return_value=None))
    monkeypatch.setattr(module.store, "finalize_continuations", MagicMock())
    service.saved = []
    monkeypatch.setattr(module.store, "save", MagicMock(side_effect=lambda *args, **_: service.saved.append(args)))
    monkeypatch.setattr(
        module.store,
        "finish",
        MagicMock(
            side_effect=lambda pair, state, report, search=None, **_: service.saved.append((pair, state, report, None))
        ),
    )
    monkeypatch.setattr(module.store, "deliver_completion_events", AsyncMock())
    yield service
    benchmark_reservations.release(-31)


def adoption():
    return {"benchmark_id": 2, "baseline_id": 1, "parameters": {"max_num_seqs": 20, "max_num_batched_tokens": 5120}}


@pytest.mark.parametrize("fails", [False, True])
async def test_improvement_is_journaled_and_confirmed_before_production_is_released(scheduler, monkeypatch, fails):
    target, snapshot, lane = scheduler.target_lane(31)
    target["model_id"] = 1
    scheduler.target_lane = lambda pair: (target, snapshot, lane)
    monkeypatch.setattr(module.store, "status", lambda _: {"enabled": True, "workload_id": "a" * 64})
    monkeypatch.setattr(
        module.workloads,
        "load",
        lambda *args: {
            "id": "a" * 64,
            "name": "Saved requests",
            "request_count": 10,
            "provenance": "synthetic",
        },
    )
    module.store.adoption_candidate.return_value = adoption()

    async def submit(*args):
        return 1, asyncio.create_task(asyncio.sleep(0, result=1))

    scheduler.submit = submit
    calls = []

    async def restore(pair, journal):
        assert not benchmark_reservations.allows(20, "model", "production")
        calls.append(dict(journal))
        if fails and journal.get("adoption"):
            raise RuntimeError("New configuration could not start")

    scheduler.restore.side_effect = restore
    finish = module.store.finish

    def commit(*args):
        assert not benchmark_reservations.allows(20, "model", "production")
        finish(*args)

    monkeypatch.setattr(module.store, "finish", commit)
    assert await scheduler.run_schedule(31, MagicMock())
    assert calls[0]["adoption"]["benchmark_id"] == 2
    report = scheduler.saved[-1][2]
    if fails:
        assert "adoption" not in calls[1]
        assert any(row["status"] == "adoption_failed" for row in report)
        assert not any(row["status"] == "adopted" for row in report)
    else:
        assert any(row["status"] == "adopted" for row in report)
    assert benchmark_reservations.allows(20, "model", "production")


@pytest.mark.parametrize("command", ["reconfigure_lane", "add_lane"])
async def test_adoption_waits_for_the_worker_status_without_cold_loading(scheduler, command):
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)
    original = {
        "max_num_seqs": 16,
        "extra_args": ["--max-num-batched-tokens=4096", "--trust-remote-code"],
        "dtype": "auto",
    }
    expected = {
        "max_num_seqs": 20,
        "extra_args": ["--trust-remote-code", "--max-num-batched-tokens", "5120"],
        "dtype": "auto",
    }
    journal = {
        "provider_id": 20,
        "model": "model",
        "lane_id": "lane",
        "vllm_config": original,
        "lane_config": {"vllm_config": original},
        "adoption": adoption(),
    }
    ready_lane = {
        "lane_id": "lane",
        "model": "model",
        "lane_config": {"vllm_config": expected},
        "runtime_state": "loaded",
        "sleep_state": "awake",
        "active_requests": 0,
    }
    answer = {"lifecycle_revision": 42}
    command_count = 0

    async def send_command(_pid, action, params, timeout_seconds=None, on_dispatched=None):
        nonlocal command_count
        if action == "get_lanes":
            return {"lanes": [{"lane_id": "lane"}] if command == "reconfigure_lane" else []}
        command_count += 1
        assert action == command
        config = params["updates"] if command == "reconfigure_lane" else params
        assert config["vllm_config"] == expected
        if on_dispatched is not None:
            future = asyncio.get_running_loop().create_future()
            on_dispatched(future)
            future.set_result(answer)
        return {**ready_lane, "restarted": command == "reconfigure_lane"}

    scheduler.registry.send_command.side_effect = send_command
    scheduler.registry.reflects_command.side_effect = [False, True, True, True]
    scheduler.registry.peek_runtime_snapshot.side_effect = [
        {"runtime": {}},  # Health check before the command.
        {"runtime": {"lanes": [{**ready_lane, "lane_config": {"vllm_config": original}}]}},
        {"runtime": {"lanes": [{**ready_lane, "runtime_state": "starting"}]}},
        {"runtime": {"lanes": [ready_lane]}},
    ]
    scheduler.planner.prepare_benchmark_lane = AsyncMock(side_effect=RuntimeError("No capacity for a cold load"))
    report = []
    await scheduler.settle(31, journal, report)

    assert report == [{"status": "adopted", **adoption()}]
    assert command_count == 1
    assert scheduler.registry.reflects_command.call_count == 4
    scheduler.registry.reflects_command.assert_called_with(20, answer)
    scheduler.planner.prepare_benchmark_lane.assert_not_called()
    assert journal["vllm_config"] == original


async def test_adoption_keeps_the_ready_lane_when_it_already_runs_the_winner(scheduler):
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)
    original = {"max_num_seqs": 16}
    current = {"max_num_seqs": 20, "extra_args": ["--max-num-batched-tokens", "5120"]}
    journal = {
        "provider_id": 20,
        "model": "model",
        "lane_id": "lane",
        "vllm_config": original,
        "lane_config": {"vllm_config": original},
        "adoption": adoption(),
    }
    scheduler.registry.send_command.return_value = {
        "lanes": [
            {
                "lane_id": "lane",
                "model": "model",
                "lane_config": {"vllm_config": current},
                "runtime_state": "loaded",
                "sleep_state": "awake",
                "active_requests": 0,
            }
        ]
    }

    report = []
    await scheduler.settle(31, journal, report)

    scheduler.registry.send_command.assert_awaited_once_with(20, "get_lanes", {}, timeout_seconds=30)
    scheduler.planner.prepare_benchmark_lane.assert_not_called()
    assert report == [{"status": "adopted", **adoption()}]


async def test_adoption_rolls_back_if_worker_never_reports_the_applied_settings_ready(scheduler, monkeypatch):
    monkeypatch.setattr(module, "LANE_READY_STATUS_TIMEOUT_SECONDS", 0.01)
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)
    original = {"max_num_seqs": 16}
    lane = {
        "lane_id": "lane",
        "model": "model",
        "lane_config": {"vllm_config": original},
        "runtime_state": "loaded",
        "sleep_state": "awake",
        "active_requests": 0,
    }
    journal = {
        "provider_id": 20,
        "model": "model",
        "lane_id": "lane",
        "vllm_config": original,
        "lane_config": lane["lane_config"],
        "adoption": adoption(),
    }
    updates = []

    async def send_command(_pid, action, params, timeout_seconds=None, on_dispatched=None):
        if action == "get_lanes":
            return {"lanes": [lane]}
        assert action == "reconfigure_lane"
        updates.append(params["updates"])
        future = asyncio.get_running_loop().create_future()
        on_dispatched(future)
        future.set_result({"lifecycle_revision": len(updates)})
        return {"restarted": True, "lane_config": params["updates"]}

    scheduler.registry.send_command.side_effect = send_command
    scheduler.registry.reflects_command.return_value = True
    scheduler.registry.peek_runtime_snapshot.return_value = {"runtime": {"lanes": [lane]}}
    report = []
    await scheduler.settle(31, journal, report)

    assert len(updates) == 2 and updates[-1] == {"vllm_config": original}
    assert report[0]["status"] == "adoption_failed"
    assert "did not report that lane lane ready with those settings" in report[0]["reason"]
    assert module.store.save.call_args.kwargs["adoption_failure"] == report[0]
    assert "adoption" not in journal


async def test_disabled_schedule_discards_pending_adoption_during_recovery(scheduler, monkeypatch):
    monkeypatch.setattr(module.store, "status", lambda _: {"enabled": False})
    journal = {"provider_id": 20, "adoption": adoption()}
    report = [{"status": "adopted", **adoption()}]
    await scheduler.settle(31, journal, report)
    assert "adoption" not in journal
    assert not report
    scheduler.restore.assert_awaited_once_with(31, journal)


async def test_confirmation_failure_keeps_worker_reserved_until_original_settings_return(scheduler):
    scheduler.restore.side_effect = [RuntimeError("Adoption failed"), RuntimeError("Rollback failed")]
    journal = {"provider_id": 20, "adoption": adoption()}
    report = []
    with pytest.raises(RuntimeError, match="Rollback failed"):
        await scheduler.settle(31, journal, report)
    assert "adoption" not in journal
    assert report[0] == {**adoption(), "status": "adoption_failed", "reason": "Adoption failed"}
    assert module.store.save.call_args.kwargs["adoption_failure"] == report[0]


async def test_pending_adoption_is_completed_after_restart(scheduler):
    journal = {"provider_id": 20, "adoption": adoption()}
    report = []
    await scheduler.settle(31, journal, report)
    assert report == [{"status": "adopted", **adoption()}]


@pytest.mark.parametrize("baseline_stalls", [False, True])
async def test_stall_retries_once_and_requires_a_successful_baseline(scheduler, baseline_stalls):
    calls = []
    scheduler._restore_lane = AsyncMock()

    async def submit(pair, settings):
        calls.append(settings.model_dump())

        async def execute():
            if (baseline_stalls and len(calls) <= 2) or (not baseline_stalls and len(calls) in {2, 3}):
                raise NightlyStopped("stalled")
            return len(calls)

        return len(calls), asyncio.create_task(execute())

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())
    if baseline_stalls:
        assert len(calls) == 2
        assert calls[0] == calls[1]
        assert scheduler.saved[-1][1] == "failed"
    else:
        assert len(calls) == 6
        assert calls[1] == calls[2]
        assert calls[3] != calls[2]
        assert scheduler.saved[-1][1] == "continuing"
    assert scheduler.saved[-1][3] is None
    assert benchmark_reservations.owner(20, "other") is None
    scheduler._restore_lane.assert_awaited()
    assert all(call.kwargs == {"force_restart": True} for call in scheduler._restore_lane.await_args_list)


async def test_cutoff_stops_remaining_configurations_and_restores(scheduler):
    async def execute():
        raise NightlyStopped("window_closed_below_half")

    scheduler.submit = AsyncMock(side_effect=lambda *args: asyncio.sleep(0))

    async def submit(*args):
        return 7, asyncio.create_task(execute())

    scheduler.submit = AsyncMock(side_effect=submit)
    assert await scheduler.run_schedule(31, MagicMock())
    assert scheduler.submit.await_count == 1
    scheduler.restore.assert_awaited_once()
    assert scheduler.saved[-1][1] == "window_closed_below_half"


async def test_recovery_failure_holds_target_model_and_stops_other_schedules(scheduler):
    scheduler.restore.side_effect = RuntimeError("worker disconnected")

    async def submit(*args):
        return 7, asyncio.create_task(asyncio.sleep(0, result=1))

    scheduler.submit = submit
    assert not await scheduler.run_schedule(31, MagicMock())
    assert scheduler.saved[-1][1] == "recovery_required"
    assert scheduler.saved[-1][3] is not None
    assert not benchmark_reservations.allows(20, "model", "production")
    assert benchmark_reservations.allows(20, "other", "production")


async def test_no_configuration_or_retry_starts_at_window_end(scheduler, monkeypatch):
    monkeypatch.setattr(module, "local_now", lambda: datetime.fromisoformat("2026-10-13T08:00:00+02:00"))
    scheduler.submit = AsyncMock()
    assert await scheduler.run_schedule(31, MagicMock())
    scheduler.submit.assert_not_awaited()
    assert scheduler.saved[-1][1] == "window_closed"


async def test_lost_cluster_lock_keeps_recovery_journal(scheduler, monkeypatch):
    async def submit(*args):
        async def run():
            monkeypatch.setattr(module.store, "lock_held", lambda _: False)
            raise NightlyStopped("lease_lost")

        return 7, asyncio.create_task(run())

    scheduler.submit = submit
    assert not await scheduler.run_schedule(31, MagicMock())
    scheduler.restore.assert_not_awaited()
    assert scheduler.saved[-1][3] is not None


async def test_lost_lease_before_preparation_never_takes_a_model_hold(scheduler, monkeypatch):
    monkeypatch.setattr(module.store, "lock_held", lambda _: False)
    scheduler.submit = AsyncMock()

    assert await scheduler.run_schedule(31, MagicMock())

    scheduler.planner._facade.reserve_model_for_drain.assert_not_called()
    scheduler.registry.send_command.assert_not_awaited()
    scheduler.submit.assert_not_awaited()
    assert scheduler.saved[-1][1] == "lease_lost"


async def test_failed_initial_drain_releases_target_without_restart(scheduler):
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)
    scheduler.recovery_worker_idle = AsyncMock(side_effect=RuntimeError("Cannot read request counts"))
    scheduler.submit = AsyncMock()
    assert await scheduler.run_schedule(31, MagicMock())
    scheduler.submit.assert_not_awaited()
    scheduler.registry.send_command.assert_awaited_once_with(20, "get_runtime", {}, timeout_seconds=30)
    assert scheduler.saved[-1][1] == "failed"
    assert scheduler.saved[-1][3] is None
    assert benchmark_reservations.allows(20, "model", "production")


@pytest.fixture
def rotation(scheduler, monkeypatch):
    rows = [
        {"model_provider_id": pair, "enabled": True, "last_run_date": None, "recovery": None, "report": []}
        for pair in (31, 32, 33)
    ]
    claimed = set()
    monkeypatch.setattr(module.store, "rows", lambda: rows)
    monkeypatch.setattr(module.store, "cluster_lock", MagicMock())
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda **kwargs: False)
    monkeypatch.setattr(module.store, "claimed_today", lambda day: len(claimed))

    def claim(pair, day, *, window):
        if pair in claimed:
            return False
        claimed.add(pair)
        return True

    monkeypatch.setattr(module.store, "claim", claim)
    scheduler.run_schedule = AsyncMock(return_value=True)
    scheduler.target_lane = MagicMock(side_effect=lambda pair: ({"provider_id": pair}, {}, {}))
    return rows, claimed


async def test_rotation_admits_two_pairs_sequentially_and_keeps_daily_cap(scheduler, rotation):
    _, claimed = rotation
    await scheduler.tick()
    await scheduler.tick()
    assert claimed == {31, 32}
    assert [call.args[0] for call in scheduler.run_schedule.await_args_list] == [31, 32]


async def test_rotation_admits_only_the_pair_with_an_open_custom_window(scheduler, rotation, monkeypatch):
    rows, claimed = rotation
    rows[1].update(window_start=time(12), window_end=time(13))
    monkeypatch.setattr(module, "local_now", lambda: datetime.fromisoformat("2026-11-25T12:05:00+01:00"))

    await scheduler.tick()

    assert claimed == {32}
    assert scheduler.run_schedule.await_args.args[0] == 32


async def test_rotation_can_claim_an_edited_window_after_a_failed_round_today(scheduler, rotation, monkeypatch):
    rows, _ = rotation
    now = datetime.fromisoformat("2026-11-25T13:10:00+01:00")
    rows[0].update(
        last_run_date=now.date(),
        status="failed",
        window_start=time(13, 10),
        window_end=time(13, 30),
        window_revision=2,
        last_run_window_revision=1,
    )
    monkeypatch.setattr(module, "local_now", lambda: now)
    monkeypatch.setattr(module.store, "claimed_today", lambda _: module.MAX_PAIRS_PER_NIGHT)

    await scheduler.tick()

    scheduler.run_schedule.assert_awaited_once()
    assert scheduler.run_schedule.await_args.args[0] == 31


async def test_metal_worker_does_not_claim_a_nightly_slot(scheduler, rotation):
    _, claimed = rotation
    runtime = scheduler.registry.peek_runtime_snapshot.return_value["runtime"]
    runtime.update(supports_nightly_gpu_budget=False, devices={"devices": [{"kind": "metal"}]})
    scheduler.target_lane.side_effect = lambda pair: (
        module.NightlyScheduler.target_lane(scheduler, pair) if pair == 31 else ({"provider_id": pair}, {}, None)
    )
    await scheduler.tick()
    assert claimed == {32, 33}
    assert scheduler.saved[0][1] == "unavailable"


async def test_unavailable_pair_does_not_consume_a_nightly_slot(scheduler, rotation):
    _, claimed = rotation
    scheduler.target_lane.side_effect = [
        RuntimeError("Worker is offline"),
        ({"provider_id": 32}, {}, {}),
        ({"provider_id": 33}, {}, {}),
    ]
    await scheduler.tick()
    assert claimed == {32, 33}
    assert scheduler.saved[0][1] == "unavailable"


async def test_capacity_deferral_retries_after_cooldown_during_the_same_window(scheduler, rotation, monkeypatch):
    rows, _ = rotation
    claimed_at = module.local_now()
    rows[0].update(last_run_date=claimed_at.date(), status=CAPACITY_WAITING, updated_at=claimed_at)
    for row in rows[1:]:
        row["enabled"] = False
    claim = MagicMock(return_value=True)
    monkeypatch.setattr(module.store, "claim", claim)

    await scheduler.tick()
    claim.assert_not_called()
    scheduler.run_schedule.assert_not_awaited()

    monkeypatch.setattr(module, "local_now", lambda: claimed_at + timedelta(seconds=CAPACITY_RETRY_SECONDS))
    await scheduler.tick()
    claim.assert_called_once_with(31, claimed_at.date(), window=MaintenanceWindow.from_env())
    assert scheduler.run_schedule.await_args.args[0] == 31


@pytest.mark.parametrize("status", [CAPACITY_WAITING, "continuing"])
async def test_claimed_round_can_retry_after_worker_reconnects(scheduler, rotation, monkeypatch, status):
    rows, _ = rotation
    claimed_at = module.local_now()
    rows[0].update(last_run_date=claimed_at.date(), status=status, updated_at=claimed_at)
    for row in rows[1:]:
        row["enabled"] = False
    claim = MagicMock(return_value=True)
    monkeypatch.setattr(module.store, "claim", claim)
    now = claimed_at + timedelta(seconds=CAPACITY_RETRY_SECONDS)
    monkeypatch.setattr(module, "local_now", lambda: now)
    scheduler.target_lane.side_effect = lambda pair: module.NightlyScheduler.target_lane(scheduler, pair)
    scheduler.registry.is_provider_online.return_value = False

    await scheduler.tick()

    module.store.save.assert_not_called()
    claim.assert_not_called()
    scheduler.run_schedule.assert_not_awaited()

    scheduler.registry.is_provider_online.return_value = True
    now += timedelta(seconds=15)
    await scheduler.tick()

    claim.assert_called_once_with(31, claimed_at.date(), window=MaintenanceWindow.from_env())
    scheduler.run_schedule.assert_awaited_once()


async def test_all_pending_recovery_precedes_any_new_pair(scheduler, rotation):
    rows, claimed = rotation
    rows[2]["recovery"] = {"provider_id": 20}
    scheduler.restore.side_effect = RuntimeError("Worker is offline")
    await scheduler.tick()
    assert claimed == set()
    scheduler.run_schedule.assert_not_awaited()


async def test_daily_quota_never_blocks_pending_recovery(scheduler, rotation):
    rows, claimed = rotation
    rows[2]["recovery"] = {"provider_id": 20}
    claimed.update({31, 32})
    await scheduler.tick()
    scheduler.restore.assert_awaited_once()
    scheduler.run_schedule.assert_not_awaited()


async def test_calibration_runs_before_benchmarks_even_without_enabled_schedules(scheduler, monkeypatch):
    events = []
    monkeypatch.setattr(module.recent_requests, "freeze", lambda *args, **kwargs: events.append("freeze"))
    scheduler.calibration = MagicMock()
    scheduler.calibration._active_provider_ids = set()
    scheduler.calibration._tick = AsyncMock(side_effect=lambda: events.append("calibration"))
    scheduler.tick = AsyncMock(side_effect=lambda: events.append("benchmarks"))
    monkeypatch.setattr(module.store, "has_recovery", lambda: False)
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda: False)
    await scheduler._cycle()
    assert events == ["freeze", "calibration", "benchmarks"]


async def test_recent_user_mode_uses_fifty_frozen_requests_in_every_configuration(scheduler, monkeypatch):
    target, snapshot, lane = scheduler.target_lane(31)
    target["model_id"] = 1
    scheduler.target_lane = lambda pair: (target, snapshot, lane)
    monkeypatch.setattr(module.recent_requests, "username", lambda: "source.user")
    source = {
        "id": "a" * 64,
        "name": "Recent requests",
        "request_count": 50,
        "provenance": "production",
        "source_selection": {"username": "source.user", "user_id": 1},
    }
    monkeypatch.setattr(module.recent_requests, "for_model", lambda *args: source)
    binding = MagicMock()
    monkeypatch.setattr(module.store, "bind_workload", binding)
    settings_sent = []

    async def submit(pair, settings):
        settings_sent.append(settings)
        return len(settings_sent), asyncio.create_task(asyncio.sleep(0, result=len(settings_sent)))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())
    binding.assert_called_once_with(31, "a" * 64)
    assert len(settings_sent) == 5
    assert all(settings.samples == 50 and settings.workload_id == source["id"] for settings in settings_sent)
    assert scheduler.saved[-1][1] == "continuing"


async def test_manual_benchmark_does_not_consume_pending_calibration_attempt(scheduler, monkeypatch):
    scheduler.calibration = MagicMock()
    scheduler.calibration._tick = AsyncMock()
    scheduler.tick = AsyncMock()
    monkeypatch.setattr(module.store, "has_recovery", lambda: False)
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda: True)
    await scheduler._cycle()
    scheduler.calibration._tick.assert_not_awaited()


async def test_pending_calibration_prevents_next_configuration(scheduler):
    scheduler.calibration = MagicMock()
    scheduler.calibration.benchmarks_must_wait.return_value = True
    scheduler.submit = AsyncMock()
    assert await scheduler.run_schedule(31, MagicMock())
    scheduler.submit.assert_not_awaited()
    scheduler.restore.assert_awaited_once()
    assert scheduler.saved[-1][1] == "calibration_pending"


async def test_automatic_calibration_claim_is_reattached_after_restart(scheduler, monkeypatch):
    scheduler.calibration = MagicMock()
    scheduler.calibration._active_provider_ids = set()
    scheduler.calibration._tick = AsyncMock()
    scheduler.maintenance = MagicMock()
    scheduler.maintenance.reconcile = AsyncMock(return_value=[])
    scheduler.tick = AsyncMock()
    monkeypatch.setattr(module.store, "calibration_claims", lambda: [{"provider_id": 20, "automatic": True}])
    monkeypatch.setattr(module.store, "has_recovery", lambda: False)
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda: False)
    await scheduler._cycle()
    assert scheduler.calibration._active_provider_ids == {20}
    scheduler.calibration._tick.assert_awaited_once()


async def test_worker_without_gpu_budget_support_does_not_start_a_nightly_round(scheduler):
    scheduler.registry.peek_runtime_snapshot.return_value["runtime"]["supports_nightly_gpu_budget"] = False
    scheduler.submit = AsyncMock()
    assert await scheduler.run_schedule(31, MagicMock())
    scheduler.submit.assert_not_awaited()
    scheduler.planner._facade.reserve_model_for_drain.assert_not_called()
    assert "Nightly GPU budgets" in scheduler.saved[-1][2][0]["reason"]


async def test_unknown_inventory_is_rejected_before_worker_is_drained(scheduler):
    scheduler.registry.peek_runtime_snapshot.return_value["runtime"]["devices"] = {}
    scheduler.submit = AsyncMock()
    assert await scheduler.run_schedule(31, MagicMock())
    scheduler.planner._facade.reserve_model_for_drain.assert_not_called()
    scheduler.planner._drain_lane.assert_not_awaited()
    scheduler.submit.assert_not_awaited()


async def test_orphaned_nightly_job_is_fenced_drained_and_recovered(scheduler, rotation, monkeypatch):
    rows, claimed = rotation
    row = rows[0]
    row["recovery"] = {"provider_id": 20, "mutated": True}
    events = []
    monkeypatch.setattr(module.store, "recovery_jobs", lambda _: [701])
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda excluding=None: excluding != [701])
    monkeypatch.setattr(module.store, "fence_recovery_jobs", lambda ids: events.append(("fence", ids)))
    monkeypatch.setattr(module.store, "finish_recovery_jobs", lambda ids: events.append(("failed", ids)))
    scheduler.recovery_worker_idle = AsyncMock(side_effect=lambda _: events.append("idle") or True)
    scheduler.restore.side_effect = lambda *args: events.append("restore")
    await scheduler.tick()
    assert events == [("fence", [701]), "idle", ("failed", [701]), "restore"]
    assert scheduler.saved[-1][1] == "interrupted_recovered"
    assert scheduler.saved[-1][3] is None
    assert not claimed


@pytest.mark.parametrize(
    "finished_at,day_offset,can_resume", [("07:59:59", 0, True), ("08:00:00", 0, False), ("05:30:00", 1, False)]
)
async def test_recovery_only_resumes_during_the_claimed_night(
    scheduler, rotation, monkeypatch, finished_at, day_offset, can_resume
):
    from datetime import timedelta

    rows, _ = rotation
    now = datetime.fromisoformat(f"2026-10-13T{finished_at}+02:00")
    monkeypatch.setattr(module, "local_now", lambda: now)
    rows[0].update(recovery={"provider_id": 20}, last_run_date=now.date() - timedelta(days=day_offset))
    scheduler.recovery_worker_idle = AsyncMock(return_value=True)

    assert await scheduler.recover(rows[0], MagicMock())
    scheduler.restore.assert_awaited_once()
    assert module.store.finish.call_args.kwargs["resume_after_recovery"] is can_resume


@pytest.mark.parametrize("blocker", ["execution", "manual", "task", "worker_busy", "lease"])
async def test_recovery_keeps_journal_when_execution_or_cleanup_is_uncertain(scheduler, rotation, monkeypatch, blocker):
    import importlib

    main = importlib.import_module("logos.main")

    rows, _ = rotation
    rows[0]["recovery"] = {"provider_id": 20}
    monkeypatch.setattr(module.store, "recovery_jobs", lambda _: [701])
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda **_: blocker == "manual")
    finish = MagicMock()
    monkeypatch.setattr(module.store, "finish_recovery_jobs", finish)
    if blocker == "execution":
        monkeypatch.setattr(module.store, "execution_lock", lambda _: nullcontext(None))
    if blocker == "task":
        monkeypatch.setitem(main._benchmark_tasks_by_job, 701, MagicMock(done=lambda: False))
    if blocker == "lease":
        monkeypatch.setattr(module.store, "execution_lock_held", lambda *args: False)
    scheduler.recovery_worker_idle = AsyncMock(return_value=blocker != "worker_busy")
    await scheduler.tick()
    finish.assert_not_called()
    scheduler.restore.assert_not_awaited()
    scheduler.run_schedule.assert_not_awaited()
    assert not scheduler.saved


@pytest.mark.parametrize(
    "runtime,expected",
    [
        (
            {
                "lanes": [
                    {
                        "model": "model",
                        "active_requests": 0,
                        "backend_metrics": {"requests_running": 0, "queue_waiting": 0},
                    }
                ]
            },
            True,
        ),
        ({"lanes": [{"model": "model", "active_requests": 1}]}, False),
        ({"lanes": [{"model": "model", "active_requests": 0, "backend_metrics": {"queue_waiting": 1}}]}, False),
        ({"lanes": [{"model": "model"}]}, False),
        ({}, False),
    ],
)
async def test_recovery_uses_fresh_worker_request_counters(scheduler, runtime, expected):
    scheduler.registry.send_command = AsyncMock(return_value=runtime)
    assert await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"}) == expected
    scheduler.registry.send_command.assert_awaited_once_with(20, "get_runtime", {}, timeout_seconds=30)


def _lane(lane_id, model, active=0, **metrics):
    return {"lane_id": lane_id, "model": model, "active_requests": active, "backend_metrics": metrics}


async def test_stale_orchestrator_inflight_counter_does_not_block_recovery(scheduler):
    scheduler.planner._facade.model_inflight_requests.return_value = 3
    scheduler.registry.send_command = AsyncMock(return_value={"lanes": [_lane("lane", "model")]})
    assert await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"})


async def test_production_traffic_on_other_lanes_does_not_block_recovery(scheduler):
    lanes = [_lane("lane", "model"), _lane("other", "other-model", 4, requests_running=4, queue_waiting=2)]
    scheduler.registry.send_command = AsyncMock(return_value={"lanes": lanes})
    assert await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"})


@pytest.mark.parametrize(
    "busy",
    [
        _lane("lane", "model", 1),
        _lane("lane", "model", requests_running=1),
        # A second lane of the recovered model, e.g. after a replacement.
        _lane("lane-2", "model", 2),
    ],
)
async def test_requests_on_the_recovered_model_block_recovery(scheduler, busy):
    lanes = [busy, _lane("other", "other-model")]
    if busy["lane_id"] != "lane":
        lanes.append(_lane("lane", "model"))
    scheduler.registry.send_command = AsyncMock(return_value={"lanes": lanes})
    assert not await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"})


async def test_missing_recovered_lane_counts_as_idle(scheduler):
    scheduler.registry.send_command = AsyncMock(return_value={"lanes": [_lane("other", "other-model", 1)]})
    assert await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"})


async def test_journal_without_lane_requires_the_whole_worker_idle(scheduler):
    scheduler.registry.send_command = AsyncMock(return_value={"lanes": [_lane("other", "other-model", 1)]})
    assert not await scheduler.recovery_worker_idle({"provider_id": 20})


async def test_unhealthy_worker_blocks_recovery(scheduler):
    scheduler.registry.send_command = AsyncMock(
        return_value={"lanes": [_lane("lane", "model")], "node_health": {"healthy": False}}
    )
    assert not await scheduler.recovery_worker_idle({"provider_id": 20, "lane_id": "lane", "model": "model"})


async def test_terminal_orphan_job_still_requires_worker_cleanup_before_restore(scheduler, rotation):
    rows, _ = rotation
    rows[0]["recovery"] = {"provider_id": 20, "mutated": True}
    scheduler.registry.send_command.return_value = {"lanes": [{"model": "model", "active_requests": 1}]}
    await scheduler.tick()
    scheduler.restore.assert_not_awaited()
    scheduler.run_schedule.assert_not_awaited()
    assert not scheduler.saved


async def test_explicit_single_worker_maintenance_runs_and_restores(scheduler, monkeypatch):
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "1")
    scheduler.registry.active_provider_ids.return_value = [20]

    async def submit(*args):
        assert not benchmark_reservations.allows(20, "model", "new-production-request")
        assert benchmark_reservations.allows(20, "other", "new-production-request")
        return 7, asyncio.create_task(asyncio.sleep(0, result=1))

    scheduler.submit = AsyncMock(side_effect=submit)
    assert await scheduler.run_schedule(31, MagicMock())
    assert scheduler.submit.await_count == 5
    scheduler.restore.assert_awaited_once()
    assert scheduler.saved[-1][1] == "continuing"
    assert benchmark_reservations.owner(20, "other") is None


def test_locked_worker_is_excluded_from_preview_without_changing_its_schedule(scheduler):
    from logos.benchmarks.nightly_overview import overview

    scheduler.planner.is_worker_locked.return_value = True
    schedule = dict(
        model_provider_id=31,
        model_id=1,
        provider_name="Worker",
        enabled=True,
        last_run_date=None,
        report=[],
        recovery=None,
    )
    result = overview([schedule], scheduler, datetime.fromisoformat("2026-10-26T12:00:00+01:00"))
    assert result["schedules"][0]["state"] == "blocked"
    assert result["schedules"][0]["reason"] == "The selected worker is locked"
    assert scheduler.saved == []


@pytest.mark.parametrize("condition", ["offline", "unhealthy", "calibrating", "cordoned"])
def test_single_worker_override_still_requires_healthy_idle_worker(scheduler, monkeypatch, condition):
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "1")
    scheduler.registry.active_provider_ids.return_value = [20]
    if condition == "offline":
        scheduler.registry.is_provider_online.return_value = False
    elif condition == "calibrating":
        scheduler.registry.is_calibrating.return_value = True
    elif condition == "cordoned":
        scheduler.registry.is_cordoned.return_value = True
    else:
        scheduler.registry.peek_runtime_snapshot.return_value["runtime"]["node_health"] = {"healthy": False}
    with pytest.raises(RuntimeError, match="selected worker must be online and healthy"):
        scheduler.target_lane(31)


async def test_two_distinct_workers_run_concurrently_with_limit_two(scheduler, rotation, monkeypatch):
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "2")
    started = set()
    both_started = asyncio.Event()

    async def run(pair, connection):
        started.add(pair)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return True

    scheduler.run_schedule.side_effect = run
    await scheduler.tick()
    assert started == {31, 32}
    assert rotation[1] == {31, 32}


async def test_two_models_on_same_worker_remain_sequential(scheduler, rotation, monkeypatch):
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "2")
    scheduler.target_lane.side_effect = lambda pair: ({"provider_id": 20}, {}, {})
    active = set()
    order = []

    async def run(pair, connection):
        assert not active
        active.add(pair)
        await asyncio.sleep(0)
        active.remove(pair)
        order.append(pair)
        return True

    scheduler.run_schedule.side_effect = run
    await scheduler.tick()
    assert order == [31, 32]


async def test_parallel_wave_failure_waits_for_sibling_cleanup(scheduler, rotation, monkeypatch):
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "2")
    running = asyncio.Event()
    cleaned = []

    async def run(pair, connection):
        if pair == 31:
            await running.wait()
            raise RuntimeError("database unavailable")
        try:
            running.set()
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleaned.append(pair)

    scheduler.run_schedule.side_effect = run
    with pytest.raises(ExceptionGroup):
        await scheduler.tick()
    assert cleaned == [32]


async def test_offline_calibration_keeps_claim_but_scheduler_cycle_continues(scheduler, monkeypatch, caplog):
    from logos.benchmarks.maintenance import MaintenanceCoordinator
    from logos.logosnode_registry import LogosNodeOfflineError

    claim = {"provider_id": 20, "token": "offline", "automatic": True, "confirmed": True}
    monkeypatch.setattr(module.store, "calibration_claims", lambda: [claim])
    monkeypatch.setattr(module.store, "cluster_lock", lambda: nullcontext(object()))
    monkeypatch.setattr(module.store, "has_recovery", lambda: False)
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda: False)
    release = MagicMock()
    monkeypatch.setattr(module.store, "release_calibration", release)
    scheduler.registry.send_command.side_effect = LogosNodeOfflineError("offline")
    scheduler.maintenance = MaintenanceCoordinator(scheduler.registry)
    scheduler.calibration = MagicMock()
    scheduler.calibration._tick = AsyncMock()
    scheduler.tick = AsyncMock()
    await scheduler._cycle()
    release.assert_not_called()
    assert scheduler.calibration._active_provider_ids == {20}
    scheduler.calibration._tick.assert_awaited_once()
    scheduler.tick.assert_awaited_once()
    assert "remains reserved" in caplog.text


async def test_recovery_of_crashed_parallel_wave_excludes_only_orphaned_sibling_jobs(scheduler, rotation, monkeypatch):
    rows, _ = rotation
    rows[0]["recovery"] = {"provider_id": 20, "mutated": True}
    rows[1]["recovery"] = {"provider_id": 21, "mutated": True}
    monkeypatch.setattr(module.store, "recovery_jobs", lambda pair: {31: [7], 32: [8]}[pair])
    checks = []
    monkeypatch.setattr(module.store, "has_active_benchmarks", lambda excluding: checks.append(excluding) or False)
    scheduler.recovery_worker_idle = AsyncMock(return_value=True)
    assert await scheduler.recover(rows[0], MagicMock())
    assert checks == [[7, 8]]
    scheduler.restore.assert_awaited_once_with(31, rows[0]["recovery"])


async def test_live_sibling_execution_prevents_parallel_wave_recovery(scheduler, rotation, monkeypatch):
    rows, _ = rotation
    rows[0]["recovery"] = {"provider_id": 20}
    rows[1]["recovery"] = {"provider_id": 21}
    monkeypatch.setattr(module.store, "execution_lock", lambda pair: nullcontext(None if pair == 32 else object()))
    assert not await scheduler.recover(rows[0], MagicMock())
    scheduler.restore.assert_not_awaited()


async def test_recovery_completes_although_the_orchestrator_counter_is_stale(scheduler, rotation):
    rows, _ = rotation
    original = {"max_num_seqs": 16, "max_num_batched_tokens": 4096}
    recovery = {
        "provider_id": 20,
        "model": "model",
        "lane_id": "lane",
        "vllm_config": original,
        "lane_config": {"lane_id": "lane", "model": "model", "vllm": True, "vllm_config": original},
        "mutated": True,
    }
    rows[0]["recovery"] = recovery
    idle_lane = {
        "lane_id": "lane",
        "model": "model",
        "lane_config": recovery["lane_config"],
        "runtime_state": "running",
        "sleep_state": "unsupported",
        "active_requests": 0,
        "backend_metrics": {},
    }

    async def send_command(_pid, action, params, timeout_seconds=None, on_dispatched=None):
        if action in {"get_runtime", "get_lanes"}:
            return {"lanes": [idle_lane]}
        assert action == "reconfigure_lane" and params["updates"] == {"vllm_config": original}
        future = asyncio.get_running_loop().create_future()
        on_dispatched(future)
        future.set_result({"lifecycle_revision": 2})
        return {**idle_lane, "restarted": True}

    scheduler.registry.send_command = AsyncMock(side_effect=send_command)
    scheduler.registry.peek_runtime_snapshot.return_value = {"runtime": {"lanes": [idle_lane]}}
    scheduler.registry.reflects_command.return_value = True
    scheduler.planner._facade.model_inflight_requests.return_value = 2  # never drops: stale
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)

    await asyncio.wait_for(scheduler.tick(), timeout=5)

    scheduler.planner.prepare_benchmark_lane.assert_not_called()
    assert scheduler.saved[-1][1:] == ("interrupted_recovered", [], None)
    assert benchmark_reservations.owner(20, "model") is None


async def test_restored_rounds_rotate_without_admitting_a_third_pair(scheduler, rotation, monkeypatch):
    rows, claimed = rotation
    day = module.local_now().date()

    def claim(pair, date, *, window):
        row = next(item for item in rows if item["model_provider_id"] == pair)
        if pair in claimed and row.get("status") != "continuing":
            return False
        claimed.add(pair)
        row.update(last_run_date=date, status="running")
        return True

    async def restored(pair, connection):
        row = next(item for item in rows if item["model_provider_id"] == pair)
        row.update(status="continuing", recovery=None)
        return True

    monkeypatch.setattr(module.store, "claim", claim)
    scheduler.run_schedule.side_effect = restored
    await scheduler.tick()
    await scheduler.tick()
    assert claimed == {31, 32}
    assert [call.args[0] for call in scheduler.run_schedule.await_args_list] == [31, 32, 31, 32]
    assert all(row["last_run_date"] == day for row in rows[:2])


async def test_a_round_that_finishes_at_window_end_restores_and_reports_completion(scheduler, monkeypatch):
    now = datetime.fromisoformat("2026-10-13T07:59:00+02:00")
    monkeypatch.setattr(module, "local_now", lambda: now)

    async def submit(*args):
        async def execute():
            nonlocal now
            now = datetime.fromisoformat("2026-10-13T08:00:00+02:00")
            return 1

        return 7, asyncio.create_task(execute())

    scheduler.submit = AsyncMock(side_effect=submit)
    assert await scheduler.run_schedule(31, MagicMock())
    assert scheduler.submit.await_count == 1
    scheduler.restore.assert_awaited_once()
    assert scheduler.saved[-1][1] == "window_closed"


@pytest.mark.parametrize("raises", [False, True])
async def test_failed_baseline_stops_variants_and_restores_production(scheduler, raises):
    count = 0

    async def submit(*args):
        nonlocal count
        count += 1
        if raises:
            raise RuntimeError("Baseline measurement failed")
        return count, asyncio.create_task(asyncio.sleep(0, result=None if count == 1 else count))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())
    assert count == 1
    scheduler.restore.assert_awaited_once()
    assert scheduler.saved[-1][1] == "failed"
    assert benchmark_reservations.allows(20, "model", "production")


async def test_completed_winner_is_durable_before_a_later_trial_is_interrupted(scheduler, monkeypatch):
    target, snapshot, lane = scheduler.target_lane(31)
    target["model_id"] = 1
    scheduler.target_lane = lambda pair: (target, snapshot, lane)
    monkeypatch.setattr(module.store, "status", lambda _: {"enabled": True, "workload_id": "a" * 64})
    monkeypatch.setattr(
        module.workloads,
        "load",
        lambda *args: {
            "id": "a" * 64,
            "name": "Saved requests",
            "request_count": 10,
            "provenance": "synthetic",
        },
    )
    import copy

    journals = []
    monkeypatch.setattr(module.store, "save", lambda *args: journals.append(copy.deepcopy(args)))
    module.store.adoption_candidate.side_effect = lambda pair, search, report: adoption() if len(report) >= 2 else None
    submitted = 0

    async def submit(*args):
        nonlocal submitted
        submitted += 1
        if submitted == 3:
            assert journals[-1][3]["adoption"]["benchmark_id"] == 2
            raise NightlyStopped("interrupted")
        return submitted, asyncio.create_task(asyncio.sleep(0, result=submitted))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())
    assert submitted == 3
    scheduler.restore.assert_awaited_once()
    assert scheduler.restore.call_args.args[1]["adoption"]["benchmark_id"] == 2
    assert any(item.get("status") == "adopted" for item in scheduler.saved[-1][2])


def lane_status(lane_id, *, model="model", seqs=16, static=False, can_sleep=False):
    return {
        "lane_id": lane_id,
        "model": model,
        "runtime_state": "loaded",
        "sleep_state": "awake" if can_sleep else "unsupported",
        "sleep_mode_enabled": can_sleep,
        "active_requests": 0,
        "is_static": static,
        "lane_config": {
            "lane_id": lane_id,
            "model": model,
            "vllm": True,
            "vllm_config": {"max_num_seqs": seqs, "max_num_batched_tokens": 4096},
        },
    }


def simulate_worker(scheduler, lanes, *, max_lanes=0, missing_delete_succeeds=False):
    runtime = scheduler.registry.peek_runtime_snapshot.return_value["runtime"]
    runtime["lanes"] = copy.deepcopy(lanes)
    scheduler.registry.reflects_command.return_value = True
    scheduler.planner._safe_get_profiles.return_value = {}
    scheduler.planner._estimate_lane_host_ram_mb.return_value = 0
    scheduler.planner._check_host_ram_headroom_for_sleep.return_value = (True, 4096, 1024)
    scheduler.planner._build_load_params.side_effect = lambda model, lane_id, *_: {
        "model": model,
        "lane_id": lane_id,
        "vllm": True,
        "vllm_config": {"max_num_seqs": 12, "max_num_batched_tokens": 4096},
    }
    scheduler.restore = module.NightlyScheduler.restore.__get__(scheduler)
    events = []

    async def send_command(_pid, action, params, *, timeout_seconds, on_dispatched=None):
        if action == "get_runtime":
            return copy.deepcopy(runtime)
        if action == "get_lanes":
            return {"lanes": copy.deepcopy(runtime["lanes"])}
        lane_id = params["lane_id"]
        events.append((action, lane_id))
        if on_dispatched is not None:
            future = asyncio.get_running_loop().create_future()
            on_dispatched(future)
            future.set_result({"lifecycle_revision": len(events)})
        lane = next((item for item in runtime["lanes"] if item["lane_id"] == lane_id), None)
        if action == "check_nightly_gpu_budget":
            assert lane is not None
            return {"ok": True, "required_mib_by_gpu": GPU_BUDGET}
        if action == "release_nightly_gpu_budget":
            return {"ok": True}
        if action == "add_lane":
            if max_lanes and len(runtime["lanes"]) >= max_lanes:
                raise module.LogosNodeCommandError(
                    f"MAX_LANES limit reached ({max_lanes})", reason_code=NIGHTLY_GPU_BUDGET_REASON
                )
            if lane is not None:
                raise module.LogosNodeCommandError(f"Lane '{lane_id}' already exists")
            lane = lane_status(lane_id, model=params["model"])
            lane["lane_config"] = {
                key: copy.deepcopy(value)
                for key, value in params.items()
                if key not in {"nightly_gpu_budget", "nightly_restore_mib_by_gpu"}
            }
            runtime["lanes"].append(lane)
        elif action == "delete_lane":
            if lane is None and not missing_delete_succeeds:
                raise module.LogosNodeCommandError(str(KeyError(f"Lane '{lane_id}' not found")))
            if lane is not None:
                if params.get("expected_model") not in {None, lane["model"]}:
                    raise module.LogosNodeCommandError(
                        "Lane belongs to another model", reason_code="nightly_lane_identity"
                    )
                assert not lane["is_static"]
                runtime["lanes"].remove(lane)
            return {"ok": True, "lane_id": lane_id}
        elif action == "sleep_lane":
            assert lane["is_static"] and lane["sleep_mode_enabled"]
            lane.update(runtime_state="sleeping", sleep_state="sleeping")
        elif action == "wake_lane":
            assert lane["runtime_state"] == "sleeping"
            lane.update(runtime_state="loaded", sleep_state="awake")
        elif action == "reconfigure_lane":
            if lane is None:
                raise module.LogosNodeCommandError(str(KeyError(f"Lane '{lane_id}' not found")))
            lane["lane_config"].update(params["updates"])
            return {**copy.deepcopy(lane), "restarted": True}
        else:
            pytest.fail(f"Unexpected worker action {action}")
        return copy.deepcopy(lane)

    scheduler.registry.send_command.side_effect = send_command
    return runtime, events


async def test_nightly_reuses_and_restores_the_only_warm_locked_lane(scheduler):
    original = lane_status("lane")
    runtime, events = simulate_worker(scheduler, [original], max_lanes=1)
    scheduler.planner.is_lane_locked.side_effect = lambda _, lane_id: lane_id == "lane"
    submitted = []

    async def submit(_pair, settings):
        assert benchmark_reservations.pinned_lane(20, "model") == "lane"
        if not submitted:
            assert runtime["lanes"] == [original]
            assert settings.serving_overrides.max_num_seqs == 16
            assert settings.serving_overrides.max_num_batched_tokens == 4096
        else:
            config = runtime["lanes"][0]["lane_config"]["vllm_config"]
            config.update(settings.serving_overrides.model_dump(exclude_none=True))
        submitted.append(settings)
        job_id = len(submitted)
        return job_id, asyncio.create_task(asyncio.sleep(0, result=job_id))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())

    assert len(submitted) == 5
    assert runtime["lanes"] == [original]
    assert events == [
        ("check_nightly_gpu_budget", "lane"),
        ("reconfigure_lane", "lane"),
        ("release_nightly_gpu_budget", "lane"),
    ]
    assert scheduler.saved[-1][1] == "continuing"
    assert benchmark_reservations.allows(20, "model", "production")


async def test_nightly_loads_a_temporary_lane_alongside_production(scheduler):
    initial = [lane_status("other", model="other-model")]
    initial[0]["active_requests"] = 3
    runtime, events = simulate_worker(scheduler, initial)
    assert scheduler.preview_configuration(31, 20, "model", {"runtime": runtime})["max_num_seqs"] == 12
    assert not events

    async def submit(*args):
        assert [(lane["lane_id"], lane["model"]) for lane in runtime["lanes"]] == [
            ("other", "other-model"),
            ("nightly-31", "model"),
        ]
        assert benchmark_reservations.pinned_lane(20, "model") == "nightly-31"
        assert benchmark_reservations.allows(20, "other-model", "production")
        # A failed benchmark still cleans up the temporary lane.
        return 7, asyncio.create_task(asyncio.sleep(0))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())
    assert runtime["lanes"] == initial
    assert events == [
        ("add_lane", "nightly-31"),
        ("delete_lane", "nightly-31"),
        ("release_nightly_gpu_budget", "nightly-31"),
    ]
    add = next(call for call in scheduler.registry.send_command.await_args_list if call.args[1] == "add_lane")
    assert add.args[2]["nightly_gpu_budget"] is True
    assert benchmark_reservations.pinned_lane(20, "model") is None
    assert benchmark_reservations.allows(20, "other-model", "production")


def test_nightly_can_place_a_temporary_lane_away_from_shared_production_gpus(scheduler):
    original = lane_status("lane")
    peer = lane_status("peer", model="other-model")
    original["lane_config"]["gpu_devices"] = "0,1"
    peer["occupied_gpu_devices"] = "1"
    runtime, _ = simulate_worker(scheduler, [original, peer])

    assert scheduler.target_lane(31)[2] is None
    config = scheduler._temporary_config(31, 20, "model", runtime["lanes"])
    assert config["gpu_devices"] == ""
    assert config["vllm_config"] == original["lane_config"]["vllm_config"]


async def test_nightly_defers_when_a_temporary_lane_does_not_fit(scheduler):
    initial = [lane_status("other", model="other-model")]
    runtime, events = simulate_worker(scheduler, initial, max_lanes=1)
    scheduler.submit = AsyncMock()

    assert await scheduler.run_schedule(31, MagicMock())

    scheduler.submit.assert_not_awaited()
    assert runtime["lanes"] == initial
    assert all(lane_id == "nightly-31" for _, lane_id in events)
    assert events[0] == ("add_lane", "nightly-31")
    assert events[-1] == ("release_nightly_gpu_budget", "nightly-31")
    assert scheduler.saved[-1][1] == CAPACITY_WAITING
    assert scheduler.saved[-1][2][-1] == {"status": CAPACITY_WAITING, "reason": "MAX_LANES limit reached (1)"}
    assert benchmark_reservations.provider_owner(20) is None


async def test_capacity_deferral_during_a_trial_restores_without_adopting(scheduler, monkeypatch):
    target, snapshot, lane = scheduler.target_lane(31)
    target["model_id"] = 1
    scheduler.target_lane = lambda _: (target, snapshot, lane)
    monkeypatch.setattr(module.store, "status", lambda _: {"enabled": True, "workload_id": "a" * 64})
    monkeypatch.setattr(
        module.workloads,
        "load",
        lambda *args: {"id": "a" * 64, "name": "Saved requests", "request_count": 10, "provenance": "synthetic"},
    )
    module.store.adoption_candidate.side_effect = lambda pair, search, report: adoption() if len(report) >= 2 else None
    original = lane_status("lane")
    runtime, events = simulate_worker(scheduler, [original])
    submitted = 0

    async def submit(*args):
        nonlocal submitted
        submitted += 1
        if submitted == 3:
            raise NightlyCapacityDeferred("GPU headroom fell below the warm-up budget")
        if submitted == 2:
            runtime["lanes"][0]["lane_config"]["vllm_config"]["max_num_seqs"] = 20
        return submitted, asyncio.create_task(asyncio.sleep(0, result=submitted))

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())

    assert submitted == 3
    assert runtime["lanes"] == [original]
    assert events == [
        ("check_nightly_gpu_budget", "lane"),
        ("reconfigure_lane", "lane"),
        ("release_nightly_gpu_budget", "lane"),
    ]
    assert scheduler.saved[-1][1] == CAPACITY_WAITING
    assert scheduler.saved[-1][2][-1] == {
        "status": CAPACITY_WAITING,
        "reason": "GPU headroom fell below the warm-up budget",
    }
    assert benchmark_reservations.provider_owner(20) is None


async def test_nightly_recreates_the_original_lane_with_journaled_gpu_budget(scheduler):
    original = lane_status("lane")
    runtime, events = simulate_worker(scheduler, [original])

    async def submit(*_args):
        assert module.store.save.call_args.args[3]["required_mib_by_gpu"] == GPU_BUDGET
        runtime["lanes"].clear()
        raise RuntimeError("Benchmark engine failed during restart")

    scheduler.submit = submit
    assert await scheduler.run_schedule(31, MagicMock())

    command = next(call for call in scheduler.registry.send_command.await_args_list if call.args[1] == "add_lane")
    assert command.args[2] == {
        **original["lane_config"],
        "nightly_gpu_budget": True,
        "nightly_restore_mib_by_gpu": GPU_BUDGET,
    }
    assert runtime["lanes"] == [original]
    assert events == [
        ("check_nightly_gpu_budget", "lane"),
        ("add_lane", "lane"),
        ("release_nightly_gpu_budget", "lane"),
    ]


async def test_nightly_restores_only_the_benchmark_lane_after_cancellation(scheduler):
    primary = lane_status("primary", static=True)
    replica = lane_status("replica", static=True, can_sleep=True, seqs=32)
    dynamic = lane_status("dynamic", seqs=24)
    other = lane_status("other", model="other-model")
    other["active_requests"] = 1
    runtime, events = simulate_worker(scheduler, [primary, replica, dynamic, other])
    started = asyncio.Event()

    async def submit(*args):
        lanes = {lane["lane_id"]: lane for lane in runtime["lanes"]}
        assert set(lanes) == {"primary", "replica", "dynamic", "other"}
        assert lanes["replica"] == replica
        assert lanes["dynamic"] == dynamic
        assert lanes["other"] == other
        assert benchmark_reservations.pinned_lane(20, "model") == "primary"
        lanes["primary"]["lane_config"]["vllm_config"]["max_num_seqs"] = 99
        started.set()
        return 7, asyncio.create_task(asyncio.Event().wait())

    scheduler.submit = submit
    task = asyncio.create_task(scheduler.run_schedule(31, MagicMock()))
    async with asyncio.timeout(3):
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert runtime["lanes"] == [primary, replica, dynamic, other]
    assert events == [
        ("check_nightly_gpu_budget", "primary"),
        ("reconfigure_lane", "primary"),
        ("release_nightly_gpu_budget", "primary"),
    ]
    assert scheduler.saved[-1][1] == "interrupted"
    assert benchmark_reservations.allows(20, "model", "production")


async def test_recovery_waits_for_a_temporarily_removed_other_model(scheduler):
    other = lane_status("other", model="other-model")
    other["active_requests"] = 1
    simulate_worker(scheduler, [other])
    journal = {
        "provider_id": 20,
        "lane_id": "nightly-31",
        "model": "model",
        "removed_lanes": [other["lane_config"]],
    }
    assert not await scheduler.recovery_worker_idle(journal)


async def test_recovery_accepts_an_already_removed_temporary_lane(scheduler):
    _, events = simulate_worker(scheduler, [])
    journal = {
        "provider_id": 20,
        "lane_id": "nightly-31",
        "model": "model",
        "temporary_lane": True,
    }
    await scheduler.restore(31, journal)
    assert events == [("delete_lane", "nightly-31")]


async def test_scoped_recovery_does_not_delete_a_temporary_id_reused_by_another_model(scheduler):
    unrelated = lane_status("nightly-31", model="other")
    runtime, events = simulate_worker(scheduler, [unrelated])
    journal = {
        "provider_id": 20,
        "lane_id": "nightly-31",
        "model": "model",
        "temporary_lane": True,
        "model_scoped": True,
        "gpu_budget": True,
    }

    await scheduler.restore(31, journal)

    assert runtime["lanes"] == [unrelated]
    assert events == [("delete_lane", "nightly-31"), ("release_nightly_gpu_budget", "nightly-31")]
    scheduler.registry.update_desired_lane_remove.assert_not_called()
    assert benchmark_reservations.allows(20, "other", "production")


async def test_recovery_replays_sleep_and_unload_journals_after_upgrade(scheduler):
    sleeper = lane_status("sleeper", model="other", static=True, can_sleep=True)
    lost_sleeper = lane_status("lost-sleeper", static=True, can_sleep=True)
    displaced = lane_status("displaced", seqs=32)
    runtime, events = simulate_worker(
        scheduler,
        [{**sleeper, "runtime_state": "sleeping", "sleep_state": "sleeping"}, lane_status("displaced")],
    )
    journal = {
        "provider_id": 20,
        "benchmark_lane_mutated": False,
        "slept_lanes": [sleeper["lane_config"], lost_sleeper["lane_config"]],
        "removed_lanes": [displaced["lane_config"]],
    }

    await scheduler.restore(31, journal)

    assert events == [
        ("reconfigure_lane", "sleeper"),
        ("wake_lane", "sleeper"),
        ("reconfigure_lane", "lost-sleeper"),
        ("add_lane", "lost-sleeper"),
        ("add_lane", "displaced"),
        ("reconfigure_lane", "displaced"),
    ]
    original = {lane["lane_id"]: lane["lane_config"] for lane in (sleeper, lost_sleeper, displaced)}
    assert {lane["lane_id"]: lane["lane_config"] for lane in runtime["lanes"]} == original
    assert all(lane["runtime_state"] == "loaded" for lane in runtime["lanes"])
    assert not benchmark_reservations.allows(20, "other", "production")


async def test_startup_fences_recovery_journals_using_their_saved_scope(scheduler, monkeypatch):
    monkeypatch.setattr(
        module.store,
        "rows",
        lambda: [
            {"model_provider_id": 31, "recovery": {"provider_id": 20, "model": "model", "model_scoped": True}},
            {"model_provider_id": 32, "recovery": {"provider_id": 21}},
        ],
    )
    scheduler._loop = AsyncMock()
    await scheduler.start()
    try:
        assert not benchmark_reservations.allows(20, "model", "production")
        assert benchmark_reservations.allows(20, "other", "production")
        assert not benchmark_reservations.allows(21, "other", "production")
    finally:
        await scheduler.stop()
        benchmark_reservations.release(-32)
