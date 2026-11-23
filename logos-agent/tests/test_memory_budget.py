from dataclasses import replace

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

        assert await manager._hold_memory(3, 4096, timeout_s=0) is True
        assert await sessions.reserved_memory_mb() == 8192
        assert await sessions.memory_admission_block(controls.Controls(session_memory_override=1024))

    async def test_without_room_the_hold_gives_up(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096)
        memory_budget.set_for_tests(budget(8192))
        monkeypatch.setattr(sessions, "_held_memory", {})

        async def reserved(_default_mb):
            return 6144

        monkeypatch.setattr(sessions.db, "reserved_memory_mb", reserved)

        assert await sessions.SessionManager()._hold_memory(3, 4096, timeout_s=0, poll_s=0) is False
        assert sessions._held_memory == {}
