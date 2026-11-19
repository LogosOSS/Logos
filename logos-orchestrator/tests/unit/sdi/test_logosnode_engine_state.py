"""Current engine observations and measurement identity for the agent runner."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from logos.logosnode_registry import LogosNodeRuntimeRegistry, ProviderSession
from logos.queue import PriorityQueueManager
from logos.sdi.logosnode_facade import LogosNodeSchedulingDataFacade


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(
        "logos.sdi.providers.logosnode_provider.LogosNodeDataProvider._load_provider_config", lambda self: {}
    )
    monkeypatch.setattr(
        "logos.sdi.providers.logosnode_provider.LogosNodeDataProvider._fetch_ps_data", lambda self: {"models": []}
    )
    registry = LogosNodeRuntimeRegistry()
    registry._sessions[13] = ProviderSession(provider_id=13, worker_id="worker", websocket=object())
    facade = LogosNodeSchedulingDataFacade(PriorityQueueManager(), runtime_registry=registry)
    facade.register_model(1, "logosnode", "http://fake", "m", 65536, provider_id=13)
    return registry, facade


def _runtime(backend=None):
    return {
        "timestamp": "2026-11-13T12:00:00Z",
        "lanes": [
            {
                "lane_id": "lane-a",
                "model": "m",
                "runtime_state": "loaded",
                "num_parallel": 4,
                "backend_metrics": backend if backend is not None else {},
            }
        ],
    }


def _state(facade):
    return facade.debug_state()["providers"]["13"]


async def test_engine_lanes_are_current_observations_separate_from_history(worker):
    registry, facade = worker
    runtime = _runtime({"requests_running": 8, "queue_waiting": 0})
    runtime["lanes"].extend(
        [
            {"lane_id": "asleep", "model": "m", "runtime_state": "sleeping"},
            {"lane_id": "stopped", "model": "m", "runtime_state": "stopped"},
            {"lane_id": "other", "model": "other", "runtime_state": "loaded"},
        ]
    )
    await registry.update_runtime(13, runtime)
    await registry.record_runtime_sample(
        13,
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scheduler_signals": {"models": {"m": {"requests_running_current": 12, "queue_waiting_current": 9}}},
        },
    )

    state = _state(facade)["models"][1]

    assert state["engine_lanes"] == [
        {"lane_id": "lane-a", "runtime_state": "loaded", "requests_running": 8.0, "queue_waiting": 0.0},
        {"lane_id": "asleep", "runtime_state": "sleeping", "requests_running": None, "queue_waiting": None},
        {"lane_id": "stopped", "runtime_state": "stopped", "requests_running": None, "queue_waiting": None},
    ]
    assert state["scheduler_signals"]["queue_waiting_current"] == 9


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (None, None),
        ("invalid", None),
        (True, None),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (0, 0.0),
        (8, 8.0),
        ("2", 2.0),
    ],
)
async def test_unknown_or_unusable_engine_gauges_stay_null(worker, reported, expected):
    registry, facade = worker
    await registry.update_runtime(13, _runtime({"requests_running": reported, "queue_waiting": reported}))

    state = _state(facade)
    lane = state["models"][1]["engine_lanes"][0]

    assert lane["requests_running"] == expected
    assert lane["queue_waiting"] == expected
    json.dumps(state, allow_nan=False)


async def test_missing_engine_metrics_are_unknown(worker):
    registry, facade = worker
    await registry.update_runtime(13, _runtime())

    lane = _state(facade)["models"][1]["engine_lanes"][0]

    assert lane["requests_running"] is None
    assert lane["queue_waiting"] is None


async def test_count_only_updates_do_not_advance_the_engine_observation_marker(worker):
    registry, facade = worker
    runtime = _runtime({"requests_running": 0, "queue_waiting": 0})
    await registry.update_runtime(13, runtime)
    first = _state(facade)["runtime"]

    count_update = copy.deepcopy(runtime)
    count_update["lanes"][0]["active_requests"] = 1
    await registry.update_runtime(13, count_update)
    await registry.mark_heartbeat(13)
    second = _state(facade)["runtime"]

    assert first["session_id"] == second["session_id"]
    assert second["runtime_revision"] > first["runtime_revision"]
    assert first["observed_at"] == second["observed_at"] == runtime["timestamp"]
    assert second["last_heartbeat"] != first["last_heartbeat"]

    next_measurement = copy.deepcopy(count_update)
    next_measurement["timestamp"] = "2026-11-13T12:00:15Z"
    next_measurement["lanes"][0]["backend_metrics"]["requests_running"] = 1
    await registry.update_runtime(13, next_measurement)
    assert _state(facade)["runtime"]["observed_at"] == next_measurement["timestamp"]


async def test_runtime_state_reports_offline_and_cordoned_workers(worker):
    registry, facade = worker
    await registry.update_runtime(13, _runtime())
    assert _state(facade)["runtime"]["online"] is True
    assert _state(facade)["runtime"]["cordoned"] is False

    registry.cordon_check = lambda provider_id: provider_id == 13
    assert _state(facade)["runtime"]["cordoned"] is True

    registry._sessions[13].last_heartbeat -= timedelta(seconds=60)
    registry._sessions[13].snapshot_version += 1
    assert _state(facade)["runtime"]["online"] is False

    await registry.detach_session(13)
    state = _state(facade)
    assert state["runtime"]["online"] is False
    assert state["runtime"]["session_id"] is None
    assert state["runtime"]["observed_at"] is None
    assert state["models"][1]["engine_lanes"] == []
