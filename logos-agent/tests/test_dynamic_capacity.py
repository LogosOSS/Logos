"""Fresh engine telemetry, dispatch pacing and caller attribution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app import capacity, controls, model_policy, sessions
from app.schemas import SessionStatus

LANE = frozenset({("1", "1")})


def engine(running=8, waiting=0, *, state="running", lane_id="lane-a"):
    return dict(lane_id=lane_id, runtime_state=state, requests_running=running, queue_waiting=waiting)


def state(*lanes, available=2, active=None, age=0):
    lanes = lanes or (engine(),)
    return {
        "logosnode": {
            "providers": {
                "1": {
                    "runtime": {
                        "session_id": "worker-session",
                        "runtime_revision": 10,
                        "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
                        "online": True,
                        "cordoned": False,
                    },
                    "models": {
                        "1": {
                            "model_name": "local-model",
                            "loaded": True,
                            "lane_parallel": 1,
                            "max_capacity": 200,
                            "active": (
                                sum(item["requests_running"] or 0 for item in lanes) if active is None else active
                            ),
                            "queue_depth": 0,
                            "engine_lanes": list(lanes),
                            "admission": {"can_admit": available > 0, "batch_limit": available},
                            "scheduler_signals": {
                                "requests_running_current": 0,
                                "queue_waiting_current": 99,
                                "gpu_cache_usage_percent_max": 99,
                            },
                        }
                    },
                }
            }
        }
    }


def provider(payload, pid="1"):
    return payload["logosnode"]["providers"][pid]


def model(payload, pid="1"):
    return provider(payload, pid)["models"]["1"]


def parse(payload, **kwargs):
    return capacity.parse_scheduler_state(payload, lane=LANE, **kwargs)


def test_dynamic_window_exceeds_full_context_guarantee_and_ignores_kv_only_pressure():
    reading = parse(state())
    assert reading.dynamic and reading.ok
    assert (reading.busy_slots, reading.total_slots, reading.available_slots) == (8, 10, 2)
    assert capacity.start_decision(reading, running=0, paused=0)[0]
    assert not capacity.pause_decision(reading)[0]
    assert not reading.saturated


def test_current_engine_queue_overrides_idle_historical_signals():
    payload = state(engine(8, 1), available=0)
    model(payload)["scheduler_signals"]["queue_waiting_current"] = 0
    reading = parse(payload)
    assert reading.queue_total == 1 and reading.saturated
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert capacity.pause_decision(reading)[0]


@pytest.mark.parametrize("sibling_state,expected_pause", [("running", False), ("loaded", False), ("sleeping", True)])
def test_only_ready_clear_sibling_prevents_global_saturation(sibling_state, expected_pause):
    reading = parse(state(engine(8, 1), engine(0, 0, state=sibling_state, lane_id="lane-b")))
    assert capacity.pause_decision(reading)[0] is expected_pause


def test_compatible_clear_provider_counts_but_unauthorized_provider_does_not():
    payload = state(engine(8, 1), available=0)
    provider_b = provider(state(engine(0), available=1))
    payload["logosnode"]["providers"]["2"] = provider_b
    assert capacity.pause_decision(parse(payload))[0]
    both = capacity.parse_scheduler_state(payload, lane=LANE | {("2", "1")})
    assert not capacity.pause_decision(both)[0]
    assert both.available_slots == 1
    assert {pid for pid, _, _ in both.sample_id} == {"2"}


def test_benchmark_waiters_do_not_hold_an_available_peer():
    payload = state(engine(3, 2), available=0, active=5)
    model(payload).update(
        active_by_api_key={"-53": 5},
        benchmark_reserved=True,
        benchmark_active=5,
        queue_depth=4,
        benchmark_queue_depth=4,
    )
    payload["logosnode"]["providers"]["2"] = provider(state(engine(1), available=2))
    # Both deployments carry the same model-wide backlog.
    model(payload, "2").update(queue_depth=4, benchmark_queue_depth=4)
    lane = LANE | {("2", "1")}

    measured = capacity.parse_scheduler_state(payload, lane=lane, own_api_key_id=7, discount_own=False)

    assert measured.queue_total == 0
    assert (measured.busy_slots, measured.available_slots) == (6, 2)
    assert capacity.start_decision(measured, running=0, paused=0)[0]
    assert not capacity.pause_decision(measured)[0]
    assert capacity.resume_decision(measured)[0]

    # A real user queued for the same model, distinct from the four benchmark requests.
    for pid in ("1", "2"):
        model(payload, pid)["queue_depth"] = 5
    queued = capacity.parse_scheduler_state(payload, lane=lane)
    assert queued.queue_total == 1
    assert capacity.start_decision(queued, running=0, paused=0) == (False, "users are queueing (1 waiting)")
    assert capacity.pause_decision(queued)[0]
    assert not capacity.resume_decision(queued)[0]


@pytest.mark.parametrize(
    "active,benchmarks,reserved,expected",
    [(5, 5, True, 0), (5, 4, True, 1), (6, 6, True, 2), (4, 4, True, 2), (5, 5, False, 2)],
)
def test_only_provable_engine_benchmark_waiters_are_discounted(active, benchmarks, reserved, expected):
    payload = state(engine(3, 2), available=0, active=active)
    model(payload).update(benchmark_reserved=reserved, benchmark_active=benchmarks)
    reading = parse(payload)
    assert reading.queue_total == expected
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert capacity.pause_decision(reading)[0] is bool(expected)


def test_benchmark_reservation_does_not_offer_headroom_or_hide_another_providers_waiters():
    payload = state(engine(2), available=2)
    model(payload).update(benchmark_reserved=True, benchmark_active=2)
    reserved = parse(payload)
    assert reserved.ok and reserved.available_slots == 0
    assert reserved.busy_slots == 2
    assert not capacity.start_decision(reserved, running=0, paused=0)[0]
    assert not capacity.resume_decision(reserved)[0]

    payload["logosnode"]["providers"]["2"] = provider(state(engine(3, 1), available=0, active=4))
    waiting = capacity.parse_scheduler_state(payload, lane=LANE | {("2", "1")})
    assert waiting.queue_total == 1
    assert capacity.pause_decision(waiting)[0]


def test_unavailable_worker_does_not_create_headroom_from_benchmark_attribution():
    payload = state(engine(3, 2), available=0, active=5, age=400)
    model(payload).update(benchmark_reserved=True, benchmark_active=5, queue_depth=4, benchmark_queue_depth=4)
    reading = parse(payload)
    assert not reading.ok
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert capacity.pause_decision(reading)[0]


def test_saturated_peer_updates_cannot_replenish_clear_peers_sample_budget():
    payload = state(engine(8, 1), available=0)
    payload["logosnode"]["providers"]["2"] = provider(state(engine(0), available=1))
    both = LANE | {("2", "1")}
    first = capacity.parse_scheduler_state(payload, lane=both)
    provider(payload)["runtime"]["observed_at"] = datetime.now(timezone.utc).isoformat()
    second = capacity.parse_scheduler_state(payload, lane=both)
    assert first.sample_id == second.sample_id
    assert first.observation_id != second.observation_id


@pytest.mark.parametrize("own,expected", [(4, 0), (3, 1)])
def test_only_attributable_engine_waiters_are_discounted(own, expected):
    payload = state(engine(3, 1), available=0, active=4)
    model(payload).update(active_by_api_key={"7": own}, queued_by_api_key={})
    measured = parse(payload, own_api_key_id=7, discount_own=False)
    adjusted = parse(payload, own_api_key_id=7)
    assert measured.queue_total == 1
    assert adjusted.queue_total == expected
    assert capacity.pause_decision(adjusted)[0] is bool(expected)
    assert adjusted.available_slots == 0


def test_own_orchestrator_backlog_does_not_pause_but_still_prevents_new_admission():
    payload = state()
    model(payload).update(queue_depth=3, queued_by_api_key={"7": 3}, active_by_api_key={"7": 8})
    assert not capacity.start_decision(parse(payload), running=1, paused=0)[0]
    adjusted = parse(payload, own_api_key_id=7)
    assert adjusted.queue_total == 0
    assert not capacity.pause_decision(adjusted)[0]


@pytest.mark.parametrize("unavailable", ["stale", "offline", "unloaded"])
def test_user_orchestrator_backlog_survives_unavailable_provider(unavailable):
    payload = state(engine(0))
    unavailable_provider = provider(deepcopy(payload))
    unavailable_model = unavailable_provider["models"]["1"]
    unavailable_model.update(model_name="other-model", queue_depth=2, loaded=False)
    if unavailable == "stale":
        unavailable_provider["runtime"]["observed_at"] = "2000-01-01T00:00:00+00:00"
    elif unavailable == "offline":
        unavailable_provider["runtime"]["online"] = False
    else:
        unavailable_model["engine_lanes"] = [engine(0, state="sleeping")]
    payload["logosnode"]["providers"]["2"] = unavailable_provider
    reading = capacity.parse_scheduler_state(payload, lane=LANE | {("2", "1")})
    assert reading.queue_total == 2
    assert capacity.pause_decision(reading)[0]


def test_owned_backlog_on_unloaded_model_is_discounted():
    payload = state(engine(0))
    extra = deepcopy(model(payload))
    extra.update(model_name="sleeping-model", loaded=False, engine_lanes=[], queue_depth=3, queued_by_api_key={"7": 3})
    provider(payload)["models"]["2"] = extra
    reading = capacity.parse_scheduler_state(payload, lane=LANE | {("1", "2")}, own_api_key_id=7)
    assert reading.queue_total == 0
    assert not capacity.pause_decision(reading)[0]


@pytest.mark.parametrize(
    "fault", ["stale", "offline", "cordoned", "missing_session", "missing_time", "missing_metric", "nan", "negative"]
)
def test_unusable_engine_observations_fail_closed(fault):
    payload = state()
    runtime = provider(payload)["runtime"]
    if fault == "stale":
        runtime["observed_at"] = "2000-01-01T00:00:00Z"
    elif fault == "offline":
        runtime["online"] = False
    elif fault == "cordoned":
        runtime["cordoned"] = True
    elif fault == "missing_session":
        runtime.pop("session_id")
    elif fault == "missing_time":
        runtime.pop("observed_at")
    else:
        model(payload)["engine_lanes"][0]["queue_waiting"] = {
            "missing_metric": None,
            "nan": float("nan"),
            "negative": -1,
        }[fault]
    reading = parse(payload)
    assert reading.dynamic and not reading.ok
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert capacity.pause_decision(reading)[0]


def test_stale_limit_can_follow_a_slower_worker_report_cadence(monkeypatch):
    payload = state(age=45)
    assert not parse(payload).ok
    monkeypatch.setattr(capacity, "settings", replace(capacity.settings, capacity_stale_after_s=60))
    assert parse(payload).ok


def test_heartbeat_and_runtime_revision_do_not_make_a_new_engine_observation():
    payload = state()
    first = parse(payload)
    provider(payload)["runtime"].update(runtime_revision=11, last_heartbeat=datetime.now(timezone.utc).isoformat())
    second = parse(payload)
    assert first.sample_id == second.sample_id
    assert first.observation_id == second.observation_id


def test_accepted_work_and_ownership_are_visible_before_engine_sample_catches_up():
    payload = state(engine(0), active=20, available=1)
    model(payload)["active_by_api_key"] = {"7": 20}
    reading = parse(payload, own_api_key_id=7, discount_own=False)
    assert (reading.busy_slots, reading.own_slots, reading.total_slots, reading.available_slots) == (20, 20, 21, 1)
    assert capacity.start_decision(reading, running=0, paused=0)[0]


def test_pacing_without_running_requests_does_not_invent_a_slot_or_pause():
    payload = state(engine(0), available=0)
    model(payload)["admission"]["reason"] = "awaiting_signal"
    reading = parse(payload)
    assert reading.ok and reading.reclaimable
    assert (reading.busy_slots, reading.total_slots, reading.available_slots) == (0, 0, 0)
    assert not capacity.pause_decision(reading)[0]
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert reading.sample_id == () and reading.observation_id


async def test_ownership_and_admission_use_one_http_snapshot(monkeypatch):
    payload = state(engine(3, 1), available=0, active=4)
    model(payload).update(active_by_api_key={"7": 4}, queued_by_api_key={})
    requests = []

    class Client:
        def __init__(self, *, timeout):
            assert timeout == 1.0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, **_kwargs):
            requests.append(url)
            return capacity.httpx.Response(200, json=payload)

    monkeypatch.setattr(capacity.httpx, "AsyncClient", Client)
    monkeypatch.setattr(capacity, "settings", replace(capacity.settings, internal_secret="test-secret"))
    measured, adjusted = await capacity.read_load_pair(lane=LANE, ours={"local-model": 1}, own_api_key_id=7)
    assert len(requests) == 1
    assert measured.queue_total == 1 and adjusted.queue_total == 0
    assert measured.observation_id == adjusted.observation_id


def test_missing_authorized_model_cannot_fall_back_to_an_unrelated_dynamic_fleet():
    reading = capacity.parse_scheduler_state(state(), lane=frozenset({("1", "missing")}))
    assert reading.dynamic and not reading.ok
    assert not capacity.start_decision(reading, running=0, paused=0)[0]


def test_revoked_local_permissions_pause_sessions_even_when_the_fleet_is_idle():
    reading = capacity.parse_scheduler_state(state(), lane=frozenset())
    assert reading.dynamic and not reading.ok
    assert capacity.pause_decision(reading)[0]
    assert not capacity.start_decision(reading, running=0, paused=0)[0]
    assert not capacity.resume_decision(reading)[0]


def test_modern_worker_without_any_model_observation_fails_closed():
    payload = state()
    provider(payload)["models"] = {}
    reading = parse(payload)
    assert reading.dynamic and not reading.ok
    assert capacity.pause_decision(reading)[0]


async def test_capacity_read_timeout_pauses_and_recovers_the_same_session(monkeypatch):
    healthy = state(available=4)
    embedder = deepcopy(model(healthy))
    embedder.update(model_name="embedder", queue_depth=6)
    provider(healthy)["models"]["2"] = embedder
    later = deepcopy(healthy)
    provider(healthy)["runtime"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

    clock = SimpleNamespace(now=0, payload=None)

    class Client:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def get(self, url, **_kwargs):
            if clock.payload is None:
                raise capacity.httpx.ReadTimeout("scheduler state timed out")
            return capacity.httpx.Response(200, json=clock.payload)

    monkeypatch.setattr(capacity.httpx, "AsyncClient", Client)
    monkeypatch.setattr(capacity, "settings", replace(capacity.settings, internal_secret="test-secret"))
    monkeypatch.setattr(sessions, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(sessions, "settings", replace(sessions.settings, capacity_resume_quiet_s=10))
    policy = model_policy.ModelPolicy(
        local_models=frozenset({"local-model", "embedder"}),
        offered=("local-model", "embedder"),
        local_deployments=LANE | {("1", "2")},
        deployments_by_model={"local-model": LANE, "embedder": frozenset({("1", "2")})},
        ok=True,
        unknown=False,
        detail="two local models",
    )
    monkeypatch.setattr(model_policy, "refresh", AsyncMock(return_value=policy))
    monkeypatch.setattr(controls, "current", AsyncMock(return_value=controls.Controls(mode="running")))

    session = {"id": 1, "model": "local-model", "container_id": "same-container"}
    session_status = SessionStatus.RUNNING

    async def rows(status):
        return [session] if session_status is status else []

    async def transition(sid, status, *, capacity_reason):
        nonlocal session_status
        assert sid == session["id"]
        if status is SessionStatus.PAUSED:
            assert capacity_reason == "capacity unknown (orchestrator unreachable)"
        session_status = status
        return True

    monkeypatch.setattr(sessions.db, "sessions_in_status", rows)
    monkeypatch.setattr(sessions.db, "transition_session", transition)
    operations = {
        name: AsyncMock(return_value=value)
        for name, value in {
            "pause_container": True,
            "unpause_container": True,
            "container_state": ("paused", None),
            "container_ip": None,
            "disconnect_network": True,
            "connect_network": True,
            "stop_container": None,
            "remove_container": None,
        }.items()
    }
    for name, operation in operations.items():
        monkeypatch.setattr(sessions.docker_engine, name, operation)
    manager = sessions.SessionManager()
    settle = AsyncMock()
    monkeypatch.setattr(manager, "_settle", settle)

    await manager._capacity_watch_pass()
    assert session_status is SessionStatus.PAUSED
    operations["pause_container"].assert_awaited_once_with("same-container")

    clock.payload = healthy
    for second in range(0, 10, 2):
        clock.now = second
        await manager._capacity_watch_pass()
        await manager.scheduler_pass()
    assert session_status is SessionStatus.PAUSED

    clock.now, clock.payload = 10, later
    await manager._capacity_watch_pass()
    await manager.scheduler_pass()
    assert session_status is SessionStatus.RUNNING
    operations["unpause_container"].assert_awaited_once_with("same-container")
    operations["stop_container"].assert_not_awaited()
    operations["remove_container"].assert_not_awaited()
    settle.assert_not_awaited()
