"""Capacity changes over time, across concurrent admissions and slow upkeep."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app import capacity, controls, model_policy, sessions
from app.schemas import SessionStatus
from tests.test_dynamic_capacity import LANE, engine, model, provider, state


@pytest.fixture
def runner(monkeypatch):
    manager = sessions.SessionManager()
    h = SimpleNamespace(
        manager=manager,
        clock=0.0,
        sample=0,
        epoch=datetime.now(timezone.utc),
        payload=state(engine(0), available=1),
        rows={},
        launched=[],
        resumed=[],
        paused=[],
        lanes=[],
    )
    provider(h.payload)["runtime"]["observed_at"] = h.epoch.isoformat()
    model(h.payload).update(active_by_api_key={}, queued_by_api_key={})
    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: h.clock))
    monkeypatch.setattr(sessions, "settings", replace(sessions.settings, capacity_resume_quiet_s=10))

    def add(sid, status, name="local-model"):
        h.rows[sid] = {"id": sid, "model": name, "status": status.value, "workspace_id": sid}

    def sample(*, waiting=0, available=1, pid="1"):
        h.sample += 1
        provider(h.payload, pid)["runtime"]["observed_at"] = (h.epoch + timedelta(microseconds=h.sample)).isoformat()
        m = model(h.payload, pid)
        m["engine_lanes"] = [engine(0, waiting)]
        m["admission"] = {"can_admit": available > 0, "batch_limit": available}

    async def rows(status):
        return [dict(row) for row in h.rows.values() if row["status"] == status.value]

    async def read_load(*, lane=None, **kwargs):
        h.lanes.append(lane)
        payload = deepcopy(h.payload)
        return replace(capacity.parse_scheduler_state(payload, lane=lane, **kwargs), snapshot=payload)

    async def queued(*, include_triggered=True):
        return next((dict(row) for row in h.rows.values() if row["status"] == SessionStatus.QUEUED.value), None)

    async def claim(sid, *, trigger_quota):
        row = h.rows[sid]
        if row["status"] != SessionStatus.QUEUED.value:
            return None
        row["status"] = SessionStatus.STARTING.value
        return dict(row)

    async def launch(row):
        h.launched.append(row["id"])
        h.rows[row["id"]]["status"] = SessionStatus.RUNNING.value

    async def resume(row, _reason):
        h.resumed.append(row["id"])
        h.rows[row["id"]]["status"] = SessionStatus.RUNNING.value
        return True

    async def pause(row, _reason):
        await asyncio.sleep(0)
        h.paused.append(row["id"])
        h.rows[row["id"]]["status"] = SessionStatus.PAUSED.value

    async def event(*_args, **_kwargs):
        pass

    monkeypatch.setattr(sessions.db, "sessions_in_status", rows)
    monkeypatch.setattr(sessions.db, "next_queued_session", queued)
    monkeypatch.setattr(sessions.db, "claim_session", claim)
    monkeypatch.setattr(sessions.db, "add_event", event)
    monkeypatch.setattr(capacity, "read_load", read_load)
    monkeypatch.setattr(manager, "_launch", launch)
    monkeypatch.setattr(manager, "_resume", resume)
    monkeypatch.setattr(manager, "_pause", pause)
    h.add, h.next_sample = add, sample
    return h


async def test_oscillating_load_restarts_quiet_window_and_resumes_one_per_observation(runner):
    h = runner
    h.add(1, SessionStatus.RUNNING)
    h.add(2, SessionStatus.RUNNING)
    h.next_sample(waiting=1, available=0)
    await h.manager._capacity_watch_pass()
    assert h.paused == [1, 2]

    h.clock = 2
    h.next_sample()
    await h.manager.scheduler_pass()
    h.clock = 11
    h.next_sample(waiting=1, available=0)
    await h.manager._capacity_watch_pass()
    h.clock = 12
    h.next_sample()
    await h.manager.scheduler_pass()
    h.clock = 21
    h.next_sample()
    await h.manager.scheduler_pass()
    assert h.resumed == []
    h.clock = 22
    await h.manager.scheduler_pass()
    assert h.resumed == [1]

    h.clock = 40
    await asyncio.gather(h.manager.scheduler_pass(), h.manager.scheduler_pass())
    assert h.resumed == [1]
    h.next_sample()
    await h.manager.scheduler_pass()
    assert h.resumed == [1, 2]
    assert h.paused == [1, 2]


async def test_elapsed_quiet_time_and_admission_flip_without_new_measurement_cannot_resume(runner):
    h = runner
    h.add(1, SessionStatus.PAUSED)
    h.next_sample(available=0)
    await h.manager.scheduler_pass()
    h.clock = 20
    model(h.payload)["admission"] = {"can_admit": True, "batch_limit": 1}
    provider(h.payload)["runtime"]["runtime_revision"] += 1
    await h.manager.scheduler_pass()
    assert h.resumed == []
    h.next_sample()
    await h.manager.scheduler_pass()
    assert h.resumed == [1]


async def test_concurrent_creation_passes_share_one_engine_sample_budget(runner):
    h = runner
    for sid in range(1, 5):
        h.add(sid, SessionStatus.QUEUED)
    await asyncio.gather(*(h.manager.scheduler_pass() for _ in range(4)))
    assert h.launched == [1]
    provider(h.payload)["runtime"]["runtime_revision"] += 1
    await h.manager.scheduler_pass()
    assert h.launched == [1]
    h.next_sample()
    await asyncio.gather(h.manager.scheduler_pass(), h.manager.scheduler_pass())
    assert h.launched == [1, 2]


async def test_saturated_peer_new_samples_cannot_buy_more_admissions(runner, monkeypatch):
    h = runner
    second = provider(state(engine(3, 1), available=0))
    h.payload["logosnode"]["providers"]["2"] = second
    current = model_policy.current()
    policy = replace(
        current, local_deployments=LANE | {("2", "1")}, deployments_by_model={"local-model": LANE | {("2", "1")}}
    )

    async def refresh():
        return policy

    monkeypatch.setattr(model_policy, "refresh", refresh)
    h.add(1, SessionStatus.QUEUED)
    h.add(2, SessionStatus.QUEUED)
    await h.manager.scheduler_pass()
    h.next_sample(waiting=1, available=0, pid="2")
    await h.manager.scheduler_pass()
    assert h.launched == [1]
    h.next_sample()
    await h.manager.scheduler_pass()
    assert h.launched == [1, 2]


async def test_resume_requires_new_measurement_of_the_sessions_actual_model(runner, monkeypatch):
    h = runner
    second = provider(state(engine(0), available=1))
    second["models"]["1"]["model_name"] = "other-model"
    h.payload["logosnode"]["providers"]["2"] = second
    policy = replace(
        model_policy.current(),
        local_models=frozenset({"local-model", "other-model"}),
        offered=("local-model", "other-model"),
        local_deployments=LANE | {("2", "1")},
        deployments_by_model={"local-model": LANE, "other-model": frozenset({("2", "1")})},
    )

    async def refresh():
        return policy

    monkeypatch.setattr(model_policy, "refresh", refresh)
    h.add(1, SessionStatus.PAUSED)
    await h.manager.scheduler_pass()
    h.clock = 11
    h.next_sample(pid="2")
    await h.manager.scheduler_pass()
    assert h.resumed == []
    assert h.lanes[-1] == LANE
    h.next_sample()
    await h.manager.scheduler_pass()
    assert h.resumed == [1]


async def test_own_waiters_and_awaiting_signal_do_not_pause_running_sessions(runner):
    h = runner
    h.add(1, SessionStatus.RUNNING)
    h.next_sample(waiting=1, available=0)
    model(h.payload).update(active=1, active_by_api_key={"7": 1}, queued_by_api_key={})
    await h.manager._capacity_watch_pass()
    assert h.paused == []
    h.next_sample(available=0)
    model(h.payload)["admission"]["reason"] = "awaiting_signal"
    await h.manager.scheduler_pass()
    assert h.paused == []


async def test_watch_and_scheduler_do_not_pause_same_session_twice(runner):
    h = runner
    h.add(1, SessionStatus.RUNNING)
    h.next_sample(waiting=1, available=0)
    await asyncio.gather(h.manager._capacity_watch_pass(), h.manager.scheduler_pass())
    assert h.paused == [1]


async def test_manual_pause_overrides_quiet_engine(runner, monkeypatch):
    h = runner
    h.add(1, SessionStatus.RUNNING)

    async def paused():
        return controls.Controls(mode=controls.PAUSED)

    monkeypatch.setattr(controls, "current", paused)
    await h.manager._capacity_watch_pass()
    assert h.paused == [1]


async def test_slow_launch_does_not_block_yield_watcher(runner, monkeypatch):
    h = runner
    h.add(1, SessionStatus.RUNNING)
    h.add(2, SessionStatus.QUEUED)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_launch(_row):
        entered.set()
        await release.wait()

    monkeypatch.setattr(h.manager, "_launch", slow_launch)
    admission = asyncio.create_task(h.manager.scheduler_pass())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        h.next_sample(waiting=1, available=0)
        await asyncio.wait_for(h.manager._capacity_watch_pass(), timeout=1)
        assert h.paused == [1]
    finally:
        release.set()
        await admission


async def test_slow_maintenance_does_not_block_capacity_watcher(runner, monkeypatch):
    h = runner
    h.add(1, SessionStatus.RUNNING)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_delivery():
        entered.set()
        await release.wait()

    monkeypatch.setattr(h.manager, "deliver_pending_replies", slow_delivery)
    maintenance = asyncio.create_task(h.manager._maintenance_loop())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        h.next_sample(waiting=1, available=0)
        await asyncio.wait_for(h.manager._capacity_watch_pass(), timeout=1)
        assert h.paused == [1]
    finally:
        maintenance.cancel()
        with pytest.raises(asyncio.CancelledError):
            await maintenance
