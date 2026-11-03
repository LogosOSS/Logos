"""Observed free VRAM cannot include memory a pending stop has yet to release."""

from types import SimpleNamespace

from logos.capacity.capacity_planner import CapacityPlanner
from logos.capacity.vram_ledger import VRAMLedger


def test_runtime_free_excludes_pending_reclaim_and_retains_consuming_debits():
    planner = object.__new__(CapacityPlanner)
    ledger = planner._vram_ledger = VRAMLedger()
    device = {"device_id": "1", "memory_free_mb": 8000}
    planner._registry = SimpleNamespace(peek_runtime_snapshot=lambda _: {"runtime": {"devices": {"devices": [device]}}})
    ledger.reserve(1, "victim", "reclaim_stop", -40000, gpu_devices="1")
    ledger.reserve(1, "other-load", "load", 1000, gpu_devices="1")
    free = planner._get_per_gpu_free(1)
    assert free == {1: 7000}
    assert planner._try_reserve_vram_atomic(1, "new", "load", 40000, 60000, gpu_devices="1", per_gpu_free=free) is None
    device["memory_free_mb"] = 48000
    assert planner._get_per_gpu_free(1) == {1: 47000}
