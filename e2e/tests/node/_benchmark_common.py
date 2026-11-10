"""Shared helpers for phased benchmark traffic / lifecycle node tests."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest
from harness.benchmark_topology import (
    BENCHMARK_CARD_VRAM_MB,
    BENCHMARK_GPUS_PER_NODE,
    BENCHMARK_NODE_COUNT,
    load_profile_fixture,
)
from harness.gpusim.scenario import GpuProfile
from harness.stack import compose
from harness.workload import validate_chat_completion

ADA = GpuProfile.load("rtx6000ada")
REQUEST_TIMEOUT_S = 300.0
_PRESSURE_MODEL = "Qwen/Qwen3.6-35B-A3B"
_TARGET_MODEL = "google/gemma-3-4b-it"


def require_bench_env() -> None:
    if os.environ.get("E2E_BENCHMARK_TOPOLOGY", "").lower() not in {"1", "true", "yes"}:
        pytest.skip(
            "set E2E_BENCHMARK_TOPOLOGY=1 after: "
            "E2E_COMPOSE_PROJECT=logos-e2e-bench E2E_ORCHESTRATOR_PORT=18190 "
            "E2E_DB_PORT=15432 python -m harness.stack.compose up --benchmark-topology "
            "&& python -m harness.stack.bootstrap_benchmark; "
            "CI should use python -m harness.stack.run_phased_benchmark"
        )
    fixture = os.environ.get("E2E_PROFILE_FIXTURE", "").strip()
    if fixture:
        load_profile_fixture(fixture)


def wait_bench_nodes():
    require_bench_env()
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES
    nodes = compose.wait_for_nodes(expected=BENCHMARK_NODE_COUNT, timeout_s=120.0)
    expected_total = round(ADA.memory_total_mb * BENCHMARK_GPUS_PER_NODE)
    ada = [n for n in nodes if round(n.total_vram_mb) == expected_total]
    assert len(ada) >= BENCHMARK_NODE_COUNT, (
        f"need ≥{BENCHMARK_NODE_COUNT} Ada nodes with {expected_total} MB; "
        f"saw {[round(n.total_vram_mb) for n in nodes]}"
    )
    return ada[:BENCHMARK_NODE_COUNT]


def sample_card_vram(admin, nodes) -> list[tuple[int, int, float, float]]:
    samples: list[tuple[int, int, float, float]] = []
    for node in nodes:
        devices = admin.node_devices(node.provider_id)
        for index, device in enumerate(devices):
            used = float(device.get("memory_used_mb") or 0.0)
            total = float(device.get("memory_total_mb") or 0.0)
            samples.append((node.provider_id, index, used, total))
    return samples


async def wait_vram_telemetry_settled(
    admin,
    nodes,
    *,
    stable_s: float = 6.0,
    timeout_s: float = 45.0,
    poll_s: float = 1.0,
) -> None:
    """Wait until every card's reported used VRAM stops changing for *stable_s*.

    Device telemetry reaches the orchestrator on a status-refresh cadence, so a
    lane that was loaded or woken moments ago is not yet reflected in the
    free-VRAM figures a planner cycle reads. Observing only, no lane actions.
    """
    deadline = time.monotonic() + timeout_s
    last: list[float] | None = None
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        used = [sample[2] for sample in await asyncio.to_thread(sample_card_vram, admin, nodes)]
        now = time.monotonic()
        if used != last:
            last = used
            stable_since = now
        elif now - stable_since >= stable_s:
            return
        await asyncio.sleep(poll_s)


def assert_vram_safety(samples, observations, *, where: str) -> None:
    assert samples, f"{where}: no device samples"
    for provider_id, index, used, total in samples:
        observations["vram_samples"] += 1
        assert total == pytest.approx(
            BENCHMARK_CARD_VRAM_MB, abs=1.0
        ), f"{where}: provider {provider_id} gpu{index} total={total} != {BENCHMARK_CARD_VRAM_MB}"
        if used > BENCHMARK_CARD_VRAM_MB + 1e-6:
            observations["vram_violations"] += 1
            raise AssertionError(
                f"{where}: VRAM oversubscription on provider {provider_id} gpu{index}: "
                f"used={used} > {BENCHMARK_CARD_VRAM_MB}"
            )


def lane_rows(admin, nodes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in nodes:
        for lane in admin.node_lanes(node.provider_id):
            row = dict(lane)
            row["_provider_id"] = node.provider_id
            rows.append(row)
    return rows


def process_pid(lane: dict[str, Any]) -> int | None:
    process = lane.get("process") or {}
    pid = process.get("pid")
    return int(pid) if pid is not None else None


def pick_node_a(bench_nodes):
    from harness.benchmark_topology import BENCHMARK_NODE_SERVICES

    for node in bench_nodes:
        if str(node.name) == BENCHMARK_NODE_SERVICES[0]:
            return node
    return bench_nodes[0]


async def chat_ok(client, developer_key: str, model: str, content: str, *, timeout_s: float | None = None) -> None:
    kwargs: dict[str, Any] = {}
    if timeout_s is not None:
        kwargs["timeout"] = timeout_s
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {developer_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 8,
            "stream": False,
        },
        **kwargs,
    )
    assert response.status_code == 200, response.text[:400]
    ok, detail, tokens, text = validate_chat_completion(response.json())
    assert ok, detail
    assert tokens >= 1 and text.strip()


def find_awake_lane(admin, provider_id: int, model: str) -> dict[str, Any] | None:
    for lane in admin.node_lanes(provider_id):
        if lane.get("model") != model:
            continue
        if str(lane.get("sleep_state") or "") == "awake" and process_pid(lane):
            return lane
    return None


def find_any_lane(admin, provider_id: int, model: str) -> dict[str, Any] | None:
    for lane in admin.node_lanes(provider_id):
        if lane.get("model") == model:
            return lane
    return None


def find_sleeping_lane(admin, provider_id: int, model: str) -> dict[str, Any] | None:
    for lane in admin.node_lanes(provider_id):
        if lane.get("model") != model:
            continue
        sleep_state = str(lane.get("sleep_state") or "")
        runtime_state = str(lane.get("runtime_state") or "")
        if sleep_state == "sleeping" or runtime_state == "sleeping":
            return lane
    return None
