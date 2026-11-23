from dataclasses import replace

import pytest
from app import controls, memory_budget


@pytest.fixture(autouse=True)
def no_budget():
    memory_budget.set_for_tests(None)
    yield
    memory_budget.set_for_tests(None)


def configured(monkeypatch, **values):
    monkeypatch.setattr(memory_budget, "settings", replace(memory_budget.settings, **values))
    monkeypatch.setattr(controls, "settings", replace(controls.settings, **values))


class TestBudget:
    def test_half_of_the_host_holds_one_session_of_four_gigabytes_on_eleven(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=4096, memory_budget_mb=0, host_memory_share=0.5)

        budget = memory_budget.compute(11_600)

        assert budget.budget_mb == 5800
        assert budget.ceiling == 1

    def test_an_explicit_budget_wins_over_the_host(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=2048, memory_budget_mb=12_288, host_memory_share=0.5)

        budget = memory_budget.compute(11_600)

        assert budget.ceiling == 6
        assert budget.source == "LOGOS_AGENT_MEMORY_BUDGET_MB"

    def test_without_the_host_and_without_a_budget_there_is_none(self, monkeypatch):
        configured(monkeypatch, memory_budget_mb=0)

        assert memory_budget.compute(None) is None

    def test_a_budget_below_one_session_holds_none(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=8192, memory_budget_mb=0, host_memory_share=0.5)

        assert memory_budget.compute(8_000).ceiling == 0

    def test_the_share_is_clamped_to_the_whole_host(self, monkeypatch):
        configured(monkeypatch, session_memory_mb=1024, memory_budget_mb=0, host_memory_share=3.0)

        assert memory_budget.compute(4096).ceiling == 4

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
        configured(monkeypatch, session_memory_mb=4096, memory_budget_mb=0, host_memory_share=0.5)

        async def host():
            return 11_600

        monkeypatch.setattr(memory_budget, "host_memory_mb", host)

        await memory_budget.establish()

        assert memory_budget.ceiling() == 1


class TestCeilingInForce:
    def test_the_memory_budget_lowers_the_configured_ceiling(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=4096)
        memory_budget.set_for_tests(memory_budget.Budget(5800, 4096, 11_600, "test"))

        assert controls.Controls().max_parallel == 1

    def test_an_operator_override_cannot_raise_it_above_the_budget(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=4096)
        memory_budget.set_for_tests(memory_budget.Budget(12_288, 4096, None, "test"))

        assert controls.Controls(max_parallel_override=8).max_parallel == 3
        assert controls.Controls(max_parallel_override=2).max_parallel == 2

    def test_without_a_budget_the_configured_ceiling_stands(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10)

        assert controls.Controls().max_parallel == 10

    def test_a_budget_that_holds_no_session_says_so(self, monkeypatch):
        configured(monkeypatch, max_parallel_sessions=10, session_memory_mb=8192)
        memory_budget.set_for_tests(memory_budget.Budget(4000, 8192, 8000, "test"))

        reason = controls.Controls().admission_block()

        assert reason.startswith("the memory budget holds no session")
        assert "8192 MB" in reason
