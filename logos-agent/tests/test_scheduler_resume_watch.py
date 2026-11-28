"""Interleave watch and scheduler passes over synthetic capacity readings."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import capacity, controls, model_policy, sessions
from app.schemas import SessionStatus

AGENT = frozenset({("15", "1")})
EMBEDDING = frozenset({("15", "2")})


@pytest.fixture
def scheduler(monkeypatch):
    policy = model_policy.ModelPolicy(
        local_models=frozenset({"agent-model", "embedder"}),
        offered=("agent-model", "embedder"),
        local_deployments=AGENT | EMBEDDING,
        deployments_by_model={"agent-model": AGENT, "embedder": EMBEDDING},
        ok=True,
        unknown=False,
        detail="two local models",
    )
    monkeypatch.setattr(model_policy, "refresh", AsyncMock(return_value=policy))
    monkeypatch.setattr(controls, "current", AsyncMock(return_value=controls.Controls(mode="running")))

    paused = [{"id": sid, "model": "agent-model"} for sid in (1, 2)]
    running = []
    in_status = AsyncMock(side_effect=lambda status: running if status is SessionStatus.RUNNING else paused)
    monkeypatch.setattr(sessions.db, "sessions_in_status", in_status)
    state = SimpleNamespace(now=0, sample=0, waiters=0)
    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: state.now))
    monkeypatch.setattr(sessions, "settings", replace(sessions.settings, capacity_resume_quiet_s=10))

    async def read_load_pair(*, lane, ours, own_api_key_id):
        sample_id = (("15", "worker", str(state.sample)),)
        reading = capacity.Reading(
            load=0.0,
            busy_slots=0,
            total_slots=4,
            queue_total=state.waiters + (6 if EMBEDDING <= lane else 0),
            ok=True,
            dynamic=True,
            available_slots=4,
            sample_id=sample_id,
            observation_id=sample_id,
        )
        return reading, reading

    read = AsyncMock(side_effect=read_load_pair)
    monkeypatch.setattr(capacity, "read_load_pair", read)

    manager = sessions.SessionManager()
    monkeypatch.setattr(manager, "_resume", AsyncMock(return_value=True))
    monkeypatch.setattr(manager, "_pause", AsyncMock())
    return manager, state, read, in_status, running, paused


async def test_unrelated_waiters_do_not_restart_the_paused_models_quiet_period(scheduler):
    manager, state, read, _, _, paused = scheduler

    for second in range(0, 10, 2):
        state.now = state.sample = second
        await manager._capacity_watch_pass()
        await manager.scheduler_pass()
    manager._resume.assert_not_awaited()

    state.now = state.sample = 10
    await manager._capacity_watch_pass()
    await manager.scheduler_pass()

    manager._resume.assert_awaited_once()
    assert manager._resume.await_args.args[0] == paused[0]
    assert all(item.kwargs["lane"] == AGENT for item in read.await_args_list)

    await manager.scheduler_pass()
    assert manager._resume.await_count == 1  # already spent this engine observation


async def test_waiters_on_a_paused_model_restart_quiet_and_require_a_new_sample(scheduler):
    manager, state, _, _, _, _ = scheduler
    await manager.scheduler_pass()

    # Only the watcher sees the transient backlog.
    state.now, state.waiters = 9, 1
    await manager._capacity_watch_pass()
    state.waiters, state.sample = 0, 1
    await manager.scheduler_pass()
    manager._resume.assert_not_awaited()

    state.now = 18
    await manager.scheduler_pass()
    manager._resume.assert_not_awaited()

    # Elapsed time alone still cannot spend the sample that opened this quiet period.
    state.now = 19
    await manager.scheduler_pass()
    manager._resume.assert_not_awaited()

    state.sample = 2
    await manager.scheduler_pass()
    manager._resume.assert_awaited_once()


async def test_running_session_yields_immediately_on_its_own_lane(scheduler):
    manager, state, read, in_status, running, _ = scheduler
    running.append({"id": 3, "model": "agent-model"})
    state.waiters = 1

    await manager._capacity_watch_pass()

    manager._pause.assert_awaited_once_with(running[0], "users are queueing (1 waiting)")
    assert read.await_args.kwargs["lane"] == AGENT
    assert all(call.args[0] is SessionStatus.RUNNING for call in in_status.await_args_list)
