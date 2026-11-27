"""No model may be loaded before it has been calibrated here."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from logos.capacity.capacity_planner import CapacityPlanner
from logos.sdi.models import ModelProfile


def _planner(*, metal: bool = False) -> CapacityPlanner:
    planner = CapacityPlanner.__new__(CapacityPlanner)
    snapshot = {"runtime": {"devices": {"mode": "metal"}}} if metal else None
    planner._registry = SimpleNamespace(peek_runtime_snapshot=lambda provider_id: snapshot)
    return planner


def _profile(residency_source: str | None, base_residency_mb: float = 4000.0) -> ModelProfile:
    return ModelProfile(
        model_name="org/model-a",
        engine="vllm",
        residency_source=residency_source,
        base_residency_mb=base_residency_mb,
    )


class TestProviderIsMetal:
    def test_no_registry_means_not_metal(self):
        planner = CapacityPlanner.__new__(CapacityPlanner)
        planner._registry = None
        assert planner._provider_is_metal(1) is False

    def test_no_snapshot_means_not_metal(self):
        assert _planner(metal=False)._provider_is_metal(1) is False

    def test_metal_device_mode_is_detected(self):
        assert _planner(metal=True)._provider_is_metal(1) is True

    def test_none_provider_id_is_not_metal(self):
        assert _planner(metal=True)._provider_is_metal(None) is False


class TestLoadRequiresCalibration:
    def test_calibrated_never_requires_it(self):
        assert CapacityPlanner._load_requires_calibration(_profile("calibrated")) is False

    @pytest.mark.parametrize("residency_source", [None, "hf", "override", "cached", "measured"])
    def test_every_other_source_requires_it(self, residency_source):
        """An estimate, a pinned value or a stored leftover is not a calibration."""
        assert CapacityPlanner._load_requires_calibration(_profile(residency_source)) is True

    def test_no_profile_requires_it(self):
        """A missing profile would build a load with no backend config
        (see load_lane_manually's docstring)."""
        assert CapacityPlanner._load_requires_calibration(None) is True


class TestFeasibilityGateRequiresCalibration:
    """``_passes_minimum_load_feasibility`` refuses a never-calibrated load."""

    def _capacity(self):
        return SimpleNamespace(available_vram_mb=80_000.0)

    def test_rejects_hf_precheck_profile(self):
        planner = _planner()
        planner.get_pending_vram_mb = lambda pid: 0.0
        ok = planner._passes_minimum_load_feasibility("org/model-a", _profile("hf"), self._capacity(), provider_id=1)
        assert ok is False

    def test_rejects_model_with_no_profile(self):
        planner = _planner()
        planner.get_pending_vram_mb = lambda pid: 0.0
        # "70B" would match the disk-size-from-name heuristic if it were
        # still consulted here — it must not be.
        ok = planner._passes_minimum_load_feasibility("org/model-70B", None, self._capacity(), provider_id=1)
        assert ok is False
