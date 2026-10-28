"""Stack assertions for the optional dual-Ada benchmark overlay.

Skipped unless ``E2E_BENCHMARK_TOPOLOGY=1`` — tests never own compose lifecycle.
Start with an isolated project so logos-e2e is undisturbed:

  E2E_COMPOSE_PROJECT=logos-e2e-bench E2E_ORCHESTRATOR_PORT=18190 \\
  E2E_BENCHMARK_TOPOLOGY=1 \\
    python -m harness.stack.compose up --benchmark-topology
"""

from __future__ import annotations

import os

import pytest
from harness.benchmark_topology import BENCHMARK_GPUS_PER_NODE, BENCHMARK_NODE_COUNT
from harness.gpusim.scenario import GpuProfile
from harness.stack import compose

pytestmark = pytest.mark.stack

ADA = GpuProfile.load("rtx6000ada")


@pytest.fixture(scope="module")
def bench_nodes(stack):
    if os.environ.get("E2E_BENCHMARK_TOPOLOGY", "").lower() not in {"1", "true", "yes"}:
        pytest.skip(
            "set E2E_BENCHMARK_TOPOLOGY=1 after starting the dual-Ada overlay "
            "(python -m harness.stack.compose up --benchmark-topology)"
        )
    return compose.wait_for_nodes(expected=BENCHMARK_NODE_COUNT)


def test_two_ada_nodes_with_two_gpus_each(bench_nodes):
    assert len(bench_nodes) >= BENCHMARK_NODE_COUNT
    expected_total = round(ADA.memory_total_mb * BENCHMARK_GPUS_PER_NODE)
    ada = [n for n in bench_nodes if round(n.total_vram_mb) == expected_total]
    assert len(ada) >= BENCHMARK_NODE_COUNT, (
        f"expected ≥{BENCHMARK_NODE_COUNT} nodes with {expected_total} MB; "
        f"saw {[round(n.total_vram_mb) for n in bench_nodes]}"
    )
    for node in ada[:BENCHMARK_NODE_COUNT]:
        assert len(node.devices) == BENCHMARK_GPUS_PER_NODE
        assert set(node.gpu_names) == {ADA.name}
