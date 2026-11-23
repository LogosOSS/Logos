from unittest.mock import AsyncMock, Mock

import pytest
from app import docker_engine, memory_usage


@pytest.mark.parametrize(
    "stats,expected",
    [({}, None), ({"limit": 4096}, None), ({"usage": -1}, None), ({"usage": 0}, 0), ({"usage": 123}, 123)],
)
async def test_one_shot_usage_excludes_missing_stats_and_limits(monkeypatch, stats, expected):
    request = AsyncMock(return_value=Mock(json=lambda: {"memory_stats": stats}))
    monkeypatch.setattr(docker_engine, "_request", request)
    assert await docker_engine.container_memory_usage("agent") == expected
    assert request.await_args.kwargs["params"] == {"stream": "false", "one-shot": "true"}


async def test_complete_session_samples_include_helpers_and_expire(monkeypatch):
    def container(session, name, state):
        return {"Id": name, "State": state, "Labels": {"logos.agent.session": str(session)}}

    containers = [
        container(1, "agent", "paused"),
        container(1, "verify", "running"),
        container(2, "prepare", "running"),
        container(2, "screenshot", "running"),
        container(3, "exited", "exited"),
    ]
    monkeypatch.setattr(docker_engine, "list_managed_containers", AsyncMock(return_value=containers))
    monkeypatch.setattr(
        docker_engine,
        "container_memory_usage",
        AsyncMock(side_effect=[100, 200, 300, docker_engine.DockerError(404, "gone")]),
    )
    save = AsyncMock()
    monkeypatch.setattr(memory_usage.db, "record_memory_peaks", save)
    monkeypatch.setattr(memory_usage, "monotonic", lambda: 10)
    await memory_usage.sample()
    save.assert_awaited_once_with({1: 300})
    assert memory_usage.current(1, "paused") == 300
    assert memory_usage.current(1, "succeeded") is None
    assert memory_usage.current(2, "starting") is None
    monkeypatch.setattr(memory_usage, "monotonic", lambda: 21)
    assert memory_usage.current(1, "running") is None
    containers.clear()
    await memory_usage.sample()
    assert memory_usage.current(1, "running") is None
    save.assert_awaited_once()
