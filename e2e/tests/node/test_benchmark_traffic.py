"""Phase 1: topology + 5×4 traffic observations (external runner persists state).

Does not prepare lifecycle or restart compose. After validated traffic, writes
``E2E_SIM_PHASE_STATE`` for the lifecycle phase.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from harness.benchmark_topology import (
    BENCHMARK_CARD_VRAM_MB,
    BENCHMARK_GPUS_PER_NODE,
    BENCHMARK_MODELS_5,
    BENCHMARK_TRAFFIC_PATTERNS,
    BOUNDED_REQUESTS_PER_PATTERN,
    FULL_SIZE_REPLAY,
)
from harness.stack.phase_state import ENV_INVOCATION_ID, ENV_STATE_PATH, build_traffic_state, write_state
from harness.workload import assert_each_model_completed, run_all_patterns
from tests.node._benchmark_common import (
    ADA,
    REQUEST_TIMEOUT_S,
    assert_vram_safety,
    lane_rows,
    sample_card_vram,
    wait_bench_nodes,
)

pytestmark = pytest.mark.stack


@pytest.fixture(scope="module")
def bench_nodes(stack, admin):
    return wait_bench_nodes()


@pytest.fixture(scope="module")
def observations():
    return {
        "pattern_reports": [],
        "cold_load": 0,
        "vram_samples": 0,
        "vram_violations": 0,
    }


def test_full_size_replay_is_runnable_cli_not_dict_only():
    assert FULL_SIZE_REPLAY["num_samples"] == 1319
    assert FULL_SIZE_REPLAY["scientific_claims"] is False
    assert BOUNDED_REQUESTS_PER_PATTERN < FULL_SIZE_REPLAY["num_samples"]
    from harness import workload as wl

    assert callable(wl.main)


def test_fleet_is_two_by_two_ada(bench_nodes):
    for node in bench_nodes:
        assert len(node.devices) == BENCHMARK_GPUS_PER_NODE
        assert set(node.gpu_names) == {ADA.name}
        assert ADA.memory_total_mb == pytest.approx(BENCHMARK_CARD_VRAM_MB)


@pytest.mark.asyncio
async def test_four_patterns_each_model_valid_completion(
    bench_nodes, orchestrator_url, developer_key, admin, observations
):
    async def _watch_vram(stop: asyncio.Event):
        while not stop.is_set():
            samples = sample_card_vram(admin, bench_nodes)
            assert_vram_safety(samples, observations, where="live")
            await asyncio.sleep(1.0)

    stop = asyncio.Event()
    watcher = asyncio.create_task(_watch_vram(stop))
    try:
        reports = await run_all_patterns(
            orchestrator_url,
            developer_key,
            n=max(BOUNDED_REQUESTS_PER_PATTERN, 5),
            patterns=BENCHMARK_TRAFFIC_PATTERNS,
            timeout_s=REQUEST_TIMEOUT_S,
        )
    finally:
        stop.set()
        await watcher

    observations["pattern_reports"] = reports
    assert [r.pattern for r in reports] == list(BENCHMARK_TRAFFIC_PATTERNS)
    for report in reports:
        assert_each_model_completed(report, BENCHMARK_MODELS_5)

    lanes = lane_rows(admin, bench_nodes)
    assert lanes, "no lanes after traffic — cold-load/routing never engaged"
    observations["cold_load"] = sum(
        1
        for lane in lanes
        if str(lane.get("runtime_state") or "") in {"loaded", "running", "sleeping"}
        or str(lane.get("sleep_state") or "") in {"awake", "sleeping"}
    )
    assert observations["cold_load"] >= 1, "no cold-load evidence on any lane"
    assert_vram_safety(sample_card_vram(admin, bench_nodes), observations, where="post-traffic")


def test_persist_traffic_phase_state(observations):
    """Write validated traffic facts for the external lifecycle phase."""
    path = os.environ.get(ENV_STATE_PATH, "").strip()
    if not path:
        pytest.skip(f"{ENV_STATE_PATH} unset — use harness.stack.run_phased_benchmark in CI")
    invocation = os.environ.get(ENV_INVOCATION_ID, "").strip()
    assert invocation, f"{ENV_INVOCATION_ID} required with {ENV_STATE_PATH}"
    assert observations["pattern_reports"], "traffic observations missing"
    assert observations["cold_load"] >= 1
    state = build_traffic_state(
        invocation_id=invocation,
        pattern_reports=observations["pattern_reports"],
        cold_load=observations["cold_load"],
        vram_samples=observations["vram_samples"],
        vram_violations=observations["vram_violations"],
    )
    written = write_state(state, path)
    assert written.is_file()
