from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from app import docker_engine, workspace_storage


async def test_docker_requests_volume_usage_only_and_keeps_unknown_sizes(monkeypatch):
    request = AsyncMock(
        return_value=SimpleNamespace(
            json=lambda: {
                "Volumes": [
                    {"Name": "checkout", "UsageData": {"Size": 0}},
                    {"Name": "cache", "UsageData": {"Size": 123}},
                    {"Name": "remote", "UsageData": {"Size": -1}},
                    {"Name": "missing"},
                    {"Name": "not_numeric", "UsageData": {"Size": False}},
                    {"UsageData": {"Size": 100}},
                ]
            }
        )
    )
    monkeypatch.setattr(docker_engine, "_request", request)
    assert await docker_engine.volume_usage_bytes() == {
        "checkout": 0,
        "cache": 123,
        "remote": None,
        "missing": None,
        "not_numeric": None,
    }
    request.assert_awaited_once_with("GET", "/system/df", params={"type": "volume"})


async def test_scans_are_coalesced_and_resampled_after_interval(monkeypatch):
    now = 10
    monkeypatch.setattr(workspace_storage, "time", SimpleNamespace(monotonic=lambda: now))
    scan = AsyncMock(side_effect=[{"checkout": 1}, {"checkout": 2}])
    monkeypatch.setattr(docker_engine, "volume_usage_bytes", scan)
    sampler = workspace_storage.VolumeUsage(interval=90)

    assert await asyncio.gather(sampler.read(), sampler.read()) == [{"checkout": 1}, {"checkout": 1}]
    assert scan.await_count == 1
    now = 100
    assert await sampler.read() == {"checkout": 2}
    assert scan.await_count == 2


async def test_docker_failure_returns_unknown_and_is_not_immediately_retried(monkeypatch):
    scan = AsyncMock(side_effect=httpx.ConnectError("daemon unavailable"))
    monkeypatch.setattr(docker_engine, "volume_usage_bytes", scan)
    sampler = workspace_storage.VolumeUsage()
    assert await sampler.read() is None
    assert await sampler.read() is None
    scan.assert_awaited_once()


@pytest.mark.parametrize("available", [0, 32])
def test_artifact_filesystem_measures_capacity_available_to_regular_users(monkeypatch, available):
    host = SimpleNamespace(f_frsize=4096, f_blocks=100, f_bavail=available, f_bfree=42)
    paths = []

    def statvfs(path):
        paths.append(path)
        return host

    monkeypatch.setattr(workspace_storage.os, "statvfs", statvfs)
    result = workspace_storage.artifact_filesystem()
    assert paths == [workspace_storage.settings.artifact_root]
    assert result.total_bytes == 409600
    assert result.available_bytes == available * 4096


async def test_sums_only_known_workspace_and_cache_sizes(monkeypatch):
    telemetry = {
        "first": 50,
        "logos_agent_cache_main": 15,
        "second": 0,
        "third": 70,
        "logos_agent_cache_linked": None,
        "unrelated": 1000,
    }
    monkeypatch.setattr(workspace_storage._volume_usage, "read", AsyncMock(return_value=telemetry))
    monkeypatch.setattr(workspace_storage, "artifact_filesystem", lambda: None)
    rows = [
        {"id": 3, "name": "main", "volume_name": "first"},
        {"id": 4, "name": "unlinked", "volume_name": "second"},
        {"id": 5, "name": "linked", "volume_name": "third"},
        {"id": 6, "name": "missing", "volume_name": "fourth"},
    ]
    result = await workspace_storage.read(rows)
    assert result.artifact_filesystem is None
    assert result.workspace_used_bytes == {3: 65, 4: 0, 5: None, 6: None}


async def test_storage_endpoint_requires_a_logos_admin(monkeypatch):
    from app import auth, main

    monkeypatch.setattr(main.db, "list_workspaces", AsyncMock(return_value=[]))
    monkeypatch.setattr(workspace_storage, "artifact_filesystem", lambda: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/workspace-storage")).status_code == 401
        for role, expected in [("app_admin", 403), ("app_developer", 403), ("logos_admin", 200)]:
            principal = auth.Principal("u", "u", frozenset({role}))
            monkeypatch.setitem(main.app.dependency_overrides, auth.current_principal, lambda: principal)
            response = await client.get("/workspace-storage")
            assert response.status_code == expected
        assert response.json() == {"artifact_filesystem": None, "workspace_used_bytes": {}}
