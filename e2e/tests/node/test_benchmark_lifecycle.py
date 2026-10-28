"""Phase 2: capacity-pressure sleep/wake + schema report (after external prepare).

Requires ``E2E_LIFECYCLE_PREPARED=1`` and ``E2E_SIM_PHASE_STATE`` written by the
traffic phase. Does **not** restart compose / orchestrator — that belongs to
``harness.stack.run_phased_benchmark`` / ``bootstrap_benchmark --prepare-lifecycle``.
Admin HTTP reads (and optional ``lanes/delete`` without compose) are allowed.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import pytest
from harness.benchmark_topology import BENCHMARK_IDLE_SLEEP_L1_S, BENCHMARK_MODELS_5
from harness.sim_report import build_report, residency_policy, write_report
from harness.stack.bootstrap_benchmark import (
    delete_model_lanes_on_providers,
    restrict_model_to_providers,
    seed_provider_links,
)
from harness.stack.phase_state import ENV_STATE_PATH, load_state, require_lifecycle_prepared, validate_traffic_state
from tests.node._benchmark_common import (
    _PRESSURE_MODEL,
    _TARGET_MODEL,
    REQUEST_TIMEOUT_S,
    assert_vram_safety,
    chat_ok,
    find_any_lane,
    find_awake_lane,
    find_sleeping_lane,
    pick_node_a,
    process_pid,
    sample_card_vram,
    wait_bench_nodes,
)

pytestmark = pytest.mark.stack

# Extra catalogue pins so pressure cannot divert to the peer node while the
# target sleeps (named-model ETTFT prefers cold peer ~0.5s over sleep wake ~2.5s).
_PRESSURE_PIN_MODELS = (
    _PRESSURE_MODEL,
    "google/gemma-3-12b-it",
    "meta-llama/Llama-3.1-8B-Instruct",
    "microsoft/Phi-4-reasoning",
)


@pytest.fixture(scope="module")
def bench_nodes(stack, admin):
    try:
        require_lifecycle_prepared()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    return wait_bench_nodes()


@pytest.fixture(scope="module")
def phase_state():
    try:
        require_lifecycle_prepared()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    path = os.environ.get(ENV_STATE_PATH, "").strip()
    if not path:
        pytest.skip(f"{ENV_STATE_PATH} required for lifecycle phase")
    state = load_state(path)
    validate_traffic_state(state)
    return state


@pytest.fixture(scope="module")
def observations(phase_state):
    obs = phase_state["observations"]
    return {
        "pattern_reports": validate_traffic_state(phase_state),
        "cold_load": int(obs["cold_load"]),
        "vram_samples": int(obs.get("vram_samples") or 0),
        "vram_violations": int(obs.get("vram_violations") or 0),
        "sleep": 0,
        "wake": 0,
        "wake_same_pid": False,
        "wake_pid_before": None,
        "wake_pid_after": None,
        "lifecycle_trigger": None,
        "lifecycle_setup": phase_state.get("lifecycle_setup"),
    }


def _lane_key(lane: dict[str, Any]) -> str:
    return str(lane.get("lane_uid") or lane.get("lane_id") or "")


@pytest.mark.asyncio
async def test_awake_sleep_l1_wake_same_engine_pid(
    bench_nodes, orchestrator_url, developer_key, admin, observations, phase_state
):
    """Controller sleep under capacity pressure; same engine PID on wake.

    Lifecycle SETUP is external (runner): catalogue restrict + ``lanes/delete``
    + orchestrator restart. This test only verifies isolation over HTTP and
    drives capacity pressure via ``/v1`` — never compose restart / direct sleep API.
    Manual admin unload of the pressure model is not used (planner must sleep/evict).
    """
    assert _TARGET_MODEL in BENCHMARK_MODELS_5 and _PRESSURE_MODEL in BENCHMARK_MODELS_5
    assert float(BENCHMARK_IDLE_SLEEP_L1_S) == 60.0

    node_a = pick_node_a(bench_nodes)
    provider_a = int(node_a.provider_id)
    pin_provider = provider_a
    all_pids = [int(n.provider_id) for n in bench_nodes]
    setup_facts = observations.get("lifecycle_setup") or phase_state.get("lifecycle_setup") or {}

    def _restrict_catalogue(*, pressure: bool = True) -> None:
        restrict_model_to_providers(_TARGET_MODEL, [pin_provider])
        # Always pin Qwen (TP2 pressure). Other pins only once pressure starts so
        # mid-size peers do not fill keep GPUs before the target is observed awake.
        restrict_model_to_providers(_PRESSURE_MODEL, [pin_provider])
        if pressure:
            for model in _PRESSURE_PIN_MODELS:
                if model == _PRESSURE_MODEL:
                    continue
                restrict_model_to_providers(model, [pin_provider])

    def _enforce_isolation(*, delete_foreign_pressure: bool = True, pressure: bool = True) -> None:
        """Catalogue restrict + admin lanes/delete only (no compose restart)."""
        _restrict_catalogue(pressure=pressure)
        foreign_pids = [int(n.provider_id) for n in bench_nodes if int(n.provider_id) != pin_provider]
        if any(find_any_lane(admin, pid, _TARGET_MODEL) for pid in foreign_pids):
            delete_model_lanes_on_providers(admin, _TARGET_MODEL, foreign_pids, timeout_s=30.0)
        if delete_foreign_pressure:
            models = _PRESSURE_PIN_MODELS if pressure else (_PRESSURE_MODEL,)
            for model in models:
                if any(find_any_lane(admin, pid, model) for pid in foreign_pids):
                    delete_model_lanes_on_providers(admin, model, foreign_pids, timeout_s=30.0)

    def _find_target_sleeping_matching(*, provider_id: int, lane_key: str, pid: int) -> dict[str, Any] | None:
        """Prefer the original lane/PID; accept same-PID sleep if lane_id renamed."""
        for lane in admin.node_lanes(provider_id):
            if lane.get("model") != _TARGET_MODEL:
                continue
            sleep_state = str(lane.get("sleep_state") or "")
            runtime_state = str(lane.get("runtime_state") or "")
            if sleep_state != "sleeping" and runtime_state != "sleeping":
                continue
            if process_pid(lane) == pid:
                return lane
            if lane_key and _lane_key(lane) == lane_key:
                return lane
        # Placement-agnostic fallback: any sleeping target with the same PID fleet-wide.
        for node in bench_nodes:
            candidate = find_sleeping_lane(admin, int(node.provider_id), _TARGET_MODEL)
            if candidate is not None and process_pid(candidate) == pid:
                return candidate
        return None

    try:
        # External prepare owns compose restart. Capability sync may re-insert
        # model_provider rows; tests may re-restrict + admin-delete only.
        # Do NOT admin-unload pressure on keep — planner must free VRAM via sleep/evict.
        # Do NOT delete the keep target before probe: under named-model ETTFT + LRU,
        # queue-for-best waits on a cold best-worker indefinitely instead of loading.
        # External prepare already isolated foreign lanes; we only re-restrict + delete foreign.
        _enforce_isolation(pressure=False)
        for pid in [int(n.provider_id) for n in bench_nodes if int(n.provider_id) != pin_provider]:
            foreign = find_any_lane(admin, pid, _TARGET_MODEL)
            assert foreign is None, (
                f"lifecycle isolation failed: {_TARGET_MODEL} still on provider={pid}; "
                f"external prepare + admin delete insufficient (foreign={foreign})"
            )

        async with httpx.AsyncClient(base_url=orchestrator_url.rstrip("/"), timeout=REQUEST_TIMEOUT_S) as client:
            probe_deadline = time.monotonic() + 240.0
            target_lane: dict[str, Any] | None = None
            probe_rounds = 0
            while time.monotonic() < probe_deadline:
                probe_rounds += 1
                _enforce_isolation(pressure=False)
                try:
                    await chat_ok(
                        client,
                        developer_key,
                        _TARGET_MODEL,
                        f"pre-pressure target probe {probe_rounds}",
                        timeout_s=45.0,
                    )
                except (AssertionError, httpx.HTTPError, httpx.TimeoutException):
                    await asyncio.sleep(2.0)
                    continue
                # Prefer pin_provider; else accept scheduler placement.
                found_pid = pin_provider
                found = find_awake_lane(admin, pin_provider, _TARGET_MODEL)
                if found is None:
                    for node in bench_nodes:
                        found = find_awake_lane(admin, int(node.provider_id), _TARGET_MODEL)
                        if found is not None:
                            found_pid = int(node.provider_id)
                            break
                if found is not None:
                    target_lane = found
                    pin_provider = found_pid
                    _restrict_catalogue()
                    break
                await asyncio.sleep(1.0)
            assert target_lane is not None, (
                f"target {_TARGET_MODEL} never observed awake after {probe_rounds} probes / 240s; "
                f"lanes_a={admin.node_lanes(provider_a)}; setup={setup_facts}"
            )

            pid_before = process_pid(target_lane)
            lane_before = _lane_key(target_lane)
            assert pid_before is not None
            observations["wake_pid_before"] = pid_before

            sleeping_lane: dict[str, Any] | None = None
            sleep_event = asyncio.Event()
            stop_pressure = asyncio.Event()

            async def _watch_sleep() -> None:
                nonlocal sleeping_lane
                while not stop_pressure.is_set():
                    hit = await asyncio.to_thread(
                        _find_target_sleeping_matching,
                        provider_id=pin_provider,
                        lane_key=lane_before,
                        pid=pid_before,
                    )
                    if hit is not None:
                        sleeping_lane = hit
                        sleep_event.set()
                        return
                    await asyncio.sleep(0.25)

            watcher = asyncio.create_task(_watch_sleep())
            deadline = time.monotonic() + 240.0
            pressure_rounds = 0
            try:
                while time.monotonic() < deadline and not sleep_event.is_set():
                    assert_vram_safety(
                        sample_card_vram(admin, bench_nodes),
                        observations,
                        where="pre-sleep-pressure",
                    )
                    if (
                        find_awake_lane(admin, pin_provider, _TARGET_MODEL) is None
                        and find_any_lane(admin, pin_provider, _TARGET_MODEL) is None
                    ):
                        if _find_target_sleeping_matching(
                            provider_id=pin_provider, lane_key=lane_before, pid=pid_before
                        ):
                            break
                        raise AssertionError(
                            f"target engine pid={pid_before} was removed (stop/delete) instead of "
                            f"sleep_L1 under pressure; lanes={admin.node_lanes(pin_provider)}"
                        )
                    pressure_rounds += 1
                    # Pin catalogue only; avoid deleting foreign pressure mid-load.
                    _enforce_isolation(delete_foreign_pressure=False)
                    model = _PRESSURE_PIN_MODELS[(pressure_rounds - 1) % len(_PRESSURE_PIN_MODELS)]
                    try:
                        await asyncio.wait_for(
                            chat_ok(
                                client,
                                developer_key,
                                model,
                                f"capacity pressure round {pressure_rounds}",
                                timeout_s=20.0,
                            ),
                            timeout=25.0,
                        )
                    except (AssertionError, httpx.HTTPError, httpx.TimeoutException, asyncio.TimeoutError):
                        pass
                    # Yield so the watcher samples during long requests.
                    try:
                        await asyncio.wait_for(sleep_event.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
            finally:
                stop_pressure.set()
                await watcher

            assert sleeping_lane is not None, (
                f"no controller sleep_L1 for {_TARGET_MODEL} (pin_provider={pin_provider}, "
                f"pid={pid_before}, lane={lane_before}, rounds={pressure_rounds}, "
                f"idle_cfg={BENCHMARK_IDLE_SLEEP_L1_S}s). "
                f"last lanes={admin.node_lanes(pin_provider)}"
            )
            pid_sleeping = process_pid(sleeping_lane)
            assert (
                pid_sleeping == pid_before
            ), f"engine PID changed across sleep: before={pid_before} sleeping={pid_sleeping}"
            observations["sleep"] = 1
            observations["lifecycle_trigger"] = "capacity_pressure"

            # Stop pressure; confirm sleep residency before wake (no admin deletes).
            _restrict_catalogue()
            settle_deadline = time.monotonic() + 30.0
            still: dict[str, Any] | None = None
            while time.monotonic() < settle_deadline:
                still = _find_target_sleeping_matching(provider_id=pin_provider, lane_key=lane_before, pid=pid_before)
                if still is not None:
                    break
                await asyncio.sleep(0.5)
            assert still is not None and process_pid(still) == pid_before, (
                f"sleeping target lost before wake: still={still}; " f"lanes={admin.node_lanes(pin_provider)}"
            )

            await chat_ok(client, developer_key, _TARGET_MODEL, "post-sleep wake probe")

            deadline = time.monotonic() + 90.0
            woke: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                woke = find_awake_lane(admin, pin_provider, _TARGET_MODEL)
                if woke is None:
                    for node in bench_nodes:
                        cand = find_awake_lane(admin, int(node.provider_id), _TARGET_MODEL)
                        if cand is not None and process_pid(cand) == pid_before:
                            woke = cand
                            break
                if woke is not None:
                    break
                await asyncio.sleep(0.5)
            assert woke is not None, f"model {_TARGET_MODEL} did not return to awake after request"
            pid_after = process_pid(woke)
            observations["wake_pid_after"] = pid_after
            assert pid_after == pid_before, (
                f"wake respawned engine: before={pid_before} after={pid_after} (want same PID); "
                f"lanes={admin.node_lanes(pin_provider)}"
            )
            observations["wake"] = 1
            observations["wake_same_pid"] = True
            assert_vram_safety(sample_card_vram(admin, bench_nodes), observations, where="post-wake")
    finally:
        seed_provider_links(all_pids)


def test_emit_sim_report(observations):
    path = os.environ.get("E2E_SIM_REPORT_PATH", "").strip()
    if not path:
        pytest.skip("E2E_SIM_REPORT_PATH unset — local optional; CI must set it")
    report = build_report(
        pattern_reports=observations["pattern_reports"],
        cold_load=observations["cold_load"],
        sleep=observations["sleep"],
        wake=observations["wake"],
        wake_same_pid=observations["wake_same_pid"],
        vram_observation_count=observations["vram_samples"],
        vram_violations=observations["vram_violations"],
        lifecycle_trigger=observations.get("lifecycle_trigger"),
    )
    if observations.get("lifecycle_setup"):
        report["lifecycle_setup"] = observations["lifecycle_setup"]
    written = write_report(report, path)
    assert written is not None and written.is_file()
    assert report["residency_policy"] == residency_policy()
    assert report["functional_verdict"] == "PASS", report
    if observations.get("lifecycle_trigger"):
        assert report.get("lifecycle_trigger") == observations["lifecycle_trigger"]
