import asyncio
from dataclasses import replace
from functools import partial
from unittest.mock import AsyncMock

import pytest
from app import controls, docker_engine, memory_budget, sessions

# Taken before the shared fixture replaces it for every other test.
REAL_ESTABLISH = memory_budget.establish


def configured(monkeypatch, **values):
    for module in (memory_budget, controls, sessions, docker_engine):
        monkeypatch.setattr(module, "settings", replace(module.settings, **values))


def budget(budget_mb, host_mb=None):
    return memory_budget.Budget(budget_mb, host_mb, "test")


class TestBudget:
    def test_half_of_the_host_holds_one_session_of_four_gigabytes_on_eleven(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0, host_memory_share=0.5)

        found = memory_budget.compute(11_600)

        assert found.budget_mb == 5800
        assert found.ceiling(4096) == 1
        assert found.ceiling(1024) == 5

    def test_an_explicit_budget_wins_over_the_host(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=12_288, host_memory_share=0.5)

        found = memory_budget.compute(11_600)

        assert found.ceiling(2048) == 6
        assert found.source == "LOGOS_AGENT_MEMORY_BUDGET_MB"

    def test_without_the_host_and_without_a_budget_there_is_none(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0)

        assert memory_budget.compute(None) is None

    def test_a_budget_below_one_session_holds_none(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0, host_memory_share=0.5)

        assert memory_budget.compute(8_000).ceiling(8192) == 0

    def test_the_share_is_clamped_to_the_whole_host(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0, host_memory_share=3.0)

        assert memory_budget.compute(4096).ceiling(1024) == 4

    def test_admission_counts_what_live_sessions_reserved(self):
        found = budget(8192)

        assert found.admission_block(reserved_mb=4096, session_mb=4096) == ""
        reason = found.admission_block(reserved_mb=6144, session_mb=4096)
        assert reason.startswith("the memory budget is used: 6144 of 8192 MB")

    async def test_the_docker_hosts_memory_is_read(self, monkeypatch):
        class Response:
            def json(self):
                return {"MemTotal": 12 * 1024 * 1024 * 1024}

        async def request(method, path, **_):
            assert (method, path) == ("GET", "/info")
            return Response()

        monkeypatch.setattr(memory_budget.docker_engine, "_request", request)

        assert await memory_budget.host_memory_mb() == 12 * 1024

    async def test_an_unreachable_engine_falls_back_to_this_machine(self, monkeypatch):
        async def request(*_args, **_kwargs):
            raise OSError("no socket")

        monkeypatch.setattr(memory_budget.docker_engine, "_request", request)

        assert await memory_budget.host_memory_mb() == memory_budget._local_memory_mb()

    async def test_establish_fixes_the_budget(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0, host_memory_share=0.5)

        async def host():
            return 11_600

        monkeypatch.setattr(memory_budget, "host_memory_mb", host)

        await REAL_ESTABLISH()

        assert memory_budget.ceiling(4096) == 1


class TestCeilingInForce:
    def test_the_ceiling_follows_the_memory_per_session(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(8192))

        assert controls.Controls().max_parallel == 2
        assert controls.Controls(session_memory_override=1024).max_parallel == 8
        assert controls.Controls(session_memory_override=1024).session_memory_mb == 1024

    def test_the_configured_cap_still_bounds_it(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=3, session_memory_mb=1024)
        memory_budget.set_for_tests(budget(65_536))

        assert controls.Controls().max_parallel_available == 3

    def test_an_operator_can_lower_it_but_not_raise_it(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(12_288))

        assert controls.Controls(max_parallel_override=8).max_parallel == 3
        assert controls.Controls(max_parallel_override=2).max_parallel == 2

    def test_without_a_budget_the_configured_ceiling_stands(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10)

        assert controls.Controls().max_parallel == 10

    def test_a_budget_that_holds_no_session_says_so(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=8192)
        memory_budget.set_for_tests(budget(4000, 8000))

        reason = controls.Controls().admission_block()

        assert reason.startswith("the memory budget holds no session")
        assert "8192 MB" in reason


class TestMemoryAdmission:
    async def test_a_session_that_does_not_fit_beside_the_reserved_ones_waits(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(8192))
        seen = []

        async def reserved(default_mb):
            seen.append(default_mb)
            return 6144

        monkeypatch.setattr(sessions.db, "reserved_memory_mb", reserved)

        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=2048)) == ""
        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=4096))
        # Rows started before a memory was recorded count with the default.
        assert seen == [4096, 4096]

    async def test_without_a_budget_nothing_is_held_back(self, monkeypatch):
        async def reserved(_default_mb):
            raise AssertionError("not read without a budget")

        monkeypatch.setattr(sessions.db, "reserved_memory_mb", reserved)

        assert await sessions.memory_admission_block(controls.Controls()) == ""

    async def test_helpers_get_the_memory_their_session_started_with(self, monkeypatch):
        monkeypatch.setattr(sessions, "_session_memory", {})
        sessions._remember_memory({"id": 5, "memory_mb": 2048})

        assert await sessions._session_memory_mb(5) == 2048
        assert await sessions._session_memory_mb(6) is None

    def test_a_container_without_a_recorded_memory_gets_the_default(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)

        assert docker_engine._memory_bytes(None) == 4096 * 1024 * 1024
        assert docker_engine._memory_bytes(1024) == 1024 * 1024 * 1024


class TestCeilingOfOne:
    def test_the_automation_may_take_the_only_place_while_no_person_waits(self):
        from app import triggers

        assert triggers.max_active_sessions(1) == 1
        assert triggers.max_active_sessions(1, person_waiting=True) == 0
        assert triggers.max_active_sessions(0) == 0
        assert triggers.max_active_sessions(5) == 4


class TestMemoryAfterRestart:
    async def test_a_miss_reads_the_recorded_memory_of_the_row(self, monkeypatch):
        monkeypatch.setattr(sessions, "_session_memory", {})

        async def recorded(session_id):
            return 1024 if session_id == 9 else None

        monkeypatch.setattr(sessions.db, "session_memory_mb", recorded)

        assert await sessions._session_memory_mb(9) == 1024
        assert sessions._session_memory[9] == 1024
        assert await sessions._session_memory_mb(10) is None


class TestHeldMemory:
    async def test_a_screenshot_container_holds_memory_that_admission_counts(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(8192))
        monkeypatch.setattr(sessions, "_held_memory", {})

        async def reserved(_default_mb):
            return 4096

        monkeypatch.setattr(sessions.db, "reserved_memory_mb", reserved)
        manager = sessions.SessionManager()

        token = await manager._hold_memory(4096, timeout_s=0)
        assert token is not None
        assert await sessions.reserved_memory_mb() == 8192
        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=1024))
        sessions._release(token)
        assert await sessions.reserved_memory_mb() == 4096

    async def test_without_room_the_hold_gives_up(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(8192))
        monkeypatch.setattr(sessions, "_held_memory", {})

        async def reserved(_default_mb):
            return 6144

        monkeypatch.setattr(sessions.db, "reserved_memory_mb", reserved)

        assert await sessions.SessionManager()._hold_memory(4096, timeout_s=0, poll_s=0) is None
        assert sessions._held_memory == {}

    async def test_a_hold_is_released_only_once_its_container_stopped(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        states = iter(["running", "running", "exited"])

        async def state(_container_id):
            return next(states), None

        monkeypatch.setattr(sessions.docker_engine, "container_state", state)
        token = sessions._hold(2048)

        await sessions._release_when_stopped(token, "c1", poll_s=0)

        assert sessions._held_memory == {}

    async def test_holders_release_only_their_own_hold(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        first, second = sessions._hold(2048), sessions._hold(2048)

        sessions._release(second)

        assert list(sessions._held_memory) == [first]


class TestCancellation:
    @staticmethod
    def _wire(monkeypatch, *, status="running", won=True, stop=None):
        async def get_session(session_id):
            return {"id": session_id, "status": status, "container_id": f"c{session_id}", "memory_mb": 2048}

        async def moved(*_args, **_kwargs):
            nonlocal status
            status = "cancelled"
            return won

        async def nothing(*_args, **_kwargs):
            return None

        async def gone(_container_id):
            return "gone", None

        monkeypatch.setattr(sessions.db, "get_session", get_session)
        monkeypatch.setattr(sessions.db, "transition_session", moved)
        monkeypatch.setattr(sessions.db, "add_event", nothing)
        monkeypatch.setattr(sessions.docker_engine, "unpause_container", nothing)
        monkeypatch.setattr(sessions.docker_engine, "stop_container", stop or nothing)
        monkeypatch.setattr(sessions.docker_engine, "remove_container", nothing)
        monkeypatch.setattr(sessions.docker_engine, "container_state", gone)

    async def test_a_cancelled_session_holds_its_memory_until_its_container_is_stopped(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)
        monkeypatch.setattr(sessions, "_held_memory", {})
        held_while_stopping = []

        async def stop(container_id, **_kwargs):
            held_while_stopping.append(sum(sessions._held_memory.values()))

        self._wire(monkeypatch, stop=stop)

        assert await sessions.SessionManager().cancel(7) is True
        await asyncio.gather(*sessions._release_tasks)

        assert held_while_stopping == [2048]
        assert sessions._held_memory == {}

    async def test_a_losing_cancellation_leaves_the_winners_hold_in_place(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        winner = sessions._hold(2048)
        self._wire(monkeypatch, won=False)

        assert await sessions.SessionManager().cancel(7) is False
        await asyncio.gather(*sessions._release_tasks)

        assert list(sessions._held_memory) == [winner]

    async def test_a_helper_that_failed_to_stop_keeps_the_hold(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        memory_budget.set_for_tests(budget(5800))
        manager = sessions.SessionManager()
        helper = sessions._Helper()
        helper.container_id = "helper-7"
        helper.created.set()
        helper.started.set()
        helper_inspected = asyncio.Event()
        helper_stopped = asyncio.Event()
        inspected = []

        async def moved(*_args, **_kwargs):
            # The finalizer registered its helper while the cancellation was claiming the row.
            manager._helpers[7] = helper
            return True

        async def refuse(container_id, **_kwargs):
            if container_id == helper.container_id:
                raise OSError("helper is still running")

        async def state(container_id):
            inspected.append(container_id)
            if container_id != helper.container_id:
                return "gone", None
            if not helper_inspected.is_set():
                helper_inspected.set()
                return "running", None
            await helper_stopped.wait()
            return "exited", 0

        self._wire(monkeypatch, status="finalizing", stop=refuse)
        monkeypatch.setattr(sessions.db, "transition_session", moved)
        monkeypatch.setattr(sessions.db, "reserved_memory_mb", AsyncMock(return_value=0))
        monkeypatch.setattr(sessions.docker_engine, "remove_container", refuse)
        monkeypatch.setattr(sessions.docker_engine, "container_state", state)
        monkeypatch.setattr(sessions, "_release_when_stopped", partial(sessions._release_when_stopped, poll_s=0))

        assert await manager.cancel(7)
        try:
            await asyncio.wait_for(helper_inspected.wait(), timeout=1)
            assert await sessions.memory_admission_block(controls.Controls(session_memory_override=4096))
        finally:
            helper_stopped.set()
            await asyncio.gather(*sessions._release_tasks)
        assert "c7" in inspected
        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=4096)) == ""

    async def test_a_claim_racing_cancellation_transfers_the_claimed_memory(self, monkeypatch):
        manager = sessions.SessionManager()
        self._wire(monkeypatch)
        memory_budget.set_for_tests(budget(5800))
        monkeypatch.setattr(sessions, "_held_memory", {})
        row = {"status": "queued", "container_id": None, "memory_mb": None}
        helper = sessions._Helper()
        helper.container_id = "prepare"
        helper.created.set()
        helper.started.set()
        attempts = []

        async def get_session(_sid):
            return row.copy()

        async def transition(_sid, target, *, expected_status=None, **_fields):
            if not attempts:
                row.update(status="starting", memory_mb=4096)
                manager._helpers[7] = helper
            attempts.append(expected_status)
            if expected_status is not None and expected_status.value != row["status"]:
                return False
            row["status"] = target.value
            return True

        monkeypatch.setattr(sessions.db, "get_session", get_session)
        monkeypatch.setattr(sessions.db, "transition_session", transition)
        monkeypatch.setattr(sessions.db, "reserved_memory_mb", AsyncMock(return_value=0))
        monkeypatch.setattr(sessions.docker_engine, "stop_container", AsyncMock(side_effect=OSError))
        monkeypatch.setattr(sessions.docker_engine, "remove_container", AsyncMock(side_effect=OSError))
        monkeypatch.setattr(sessions.docker_engine, "container_state", AsyncMock(return_value=("gone", None)))

        assert await manager.cancel(7)
        assert row["status"] == "cancelled"
        assert await sessions.reserved_memory_mb() == 4096
        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=4096))
        assert attempts == [sessions.SessionStatus.QUEUED, sessions.SessionStatus.STARTING]
        await asyncio.gather(*sessions._release_tasks)
        assert await sessions.reserved_memory_mb() == 0

    async def test_a_queued_session_holds_nothing(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        seen = []

        async def moved(*_args, **_kwargs):
            seen.append(dict(sessions._held_memory))
            return True

        self._wire(monkeypatch, status="queued")
        monkeypatch.setattr(sessions.db, "transition_session", moved)

        assert await sessions.SessionManager().cancel(8) is True
        assert seen == [{}]


class TestHoldLifetime:
    async def test_a_helper_that_outlives_its_own_cleanup_remains_reserved(self, monkeypatch):
        manager = sessions.SessionManager()
        monkeypatch.setattr(sessions, "_held_memory", {})
        monkeypatch.setattr(sessions, "_session_memory", {7: 3072})
        monkeypatch.setattr(sessions.docker_engine, "create_session_container", AsyncMock(return_value="helper"))
        monkeypatch.setattr(sessions.docker_engine, "start_container", AsyncMock())
        monkeypatch.setattr(sessions.docker_engine, "wait_container", AsyncMock(side_effect=OSError("wait failed")))
        monkeypatch.setattr(sessions.docker_engine, "remove_container", AsyncMock(side_effect=OSError))
        state = AsyncMock(side_effect=[("paused", None), ("gone", None)])
        monkeypatch.setattr(sessions.docker_engine, "container_state", state)
        monkeypatch.setattr(sessions, "_release_when_stopped", partial(sessions._release_when_stopped, poll_s=0))

        with pytest.raises(OSError, match="wait failed"):
            await manager._run_helper(
                phase="prepare", session_id=7, env={}, workspace_volume="volume", artifact_host_path="/artifacts/7"
            )
        assert 7 not in manager._helpers
        assert sum(sessions._held_memory.values()) == 3072
        await asyncio.gather(*sessions._release_tasks)
        assert state.await_count == 2
        assert sessions._held_memory == {}

    async def test_a_running_container_keeps_its_hold_past_any_deadline(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        states = iter(["running", "paused", "unknown", "running", "unknown", "exited"])
        checks = []
        now = 0
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: now)
        token = sessions._hold(2048)

        async def state(_container_id):
            nonlocal now
            now += 601
            checks.append((next(states), token in sessions._held_memory))
            return checks[-1][0], None

        monkeypatch.setattr(sessions.docker_engine, "container_state", state)
        await sessions._release_when_stopped(token, "c1", poll_s=0)

        assert checks == [(s, True) for s in ("running", "paused", "unknown", "running", "unknown", "exited")]
        assert sessions._held_memory == {}

    async def test_a_container_that_cannot_be_inspected_is_given_up_on(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})

        async def broken(_container_id):
            raise OSError("docker is gone")

        monkeypatch.setattr(sessions.docker_engine, "container_state", broken)
        token = sessions._hold(2048)

        await sessions._release_when_stopped(token, "c1", unknown_timeout_s=0, poll_s=0)

        assert sessions._held_memory == {}


class TestSettlement:
    async def test_a_session_settled_with_its_container_running_keeps_its_memory_until_it_stops(self, monkeypatch):
        monkeypatch.setattr(sessions, "_held_memory", {})
        states = iter(["running", "exited"])
        held_during_outcome = []

        async def get_session(session_id):
            return {"id": session_id, "status": "running", "container_id": "c5", "memory_mb": 3072}

        async def event(*_args):
            held_during_outcome.append(sum(sessions._held_memory.values()))

        async def state(_container_id):
            return next(states), None

        monkeypatch.setattr(sessions.db, "get_session", get_session)
        monkeypatch.setattr(sessions.SessionManager, "serve_github_requests", AsyncMock())
        monkeypatch.setattr(sessions.SessionManager, "_post_reply", AsyncMock())
        monkeypatch.setattr(sessions.db, "transition_session", AsyncMock(return_value=True))
        monkeypatch.setattr(sessions.db, "add_event", event)
        monkeypatch.setattr(sessions.docker_engine, "remove_container", AsyncMock(side_effect=OSError))
        monkeypatch.setattr(sessions.docker_engine, "container_state", state)
        monkeypatch.setattr(sessions, "_release_when_stopped", partial(sessions._release_when_stopped, poll_s=0))

        await sessions.SessionManager()._settle(5, exit_code=None, error="supervision failed")
        assert sum(sessions._held_memory.values()) == 3072
        await asyncio.gather(*sessions._release_tasks)

        assert held_during_outcome == [3072]
        assert sessions._held_memory == {}
