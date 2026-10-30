"""Field ownership and resolution of the central model-profile store."""

from __future__ import annotations

from logos.model_profile_store import (
    ProfileWriteCache,
    base_residency_agrees,
    calibration_snapshot,
    effective_profile,
    is_central_profile_payload,
    is_new_local_calibration,
    material_profile,
    persistable_profile,
    profile_digest,
    profile_needs_calibration,
    reported_profile,
    shared_profiles_enabled,
)


def _row(**overrides):
    row = {
        "model_name": "org/model",
        "profile": {"base_residency_mb": 15000.0, "residency_source": "calibrated"},
        "sync_revision": 3,
        "calibration_key_hash": "H1",
        "cal_id": None,
        "cal_key_hash": None,
        "cal_source_provider_id": None,
        "cal_invalidated_at": None,
    }
    row.update(overrides)
    return row


def test_only_echoes_with_a_sync_revision_are_central():
    assert is_central_profile_payload({"m": {"sync_revision": 0}})
    assert not is_central_profile_payload({"m": {"base_residency_mb": 1.0}})
    assert not is_central_profile_payload({})


def test_persistable_profile_drops_sync_metadata_and_overrides():
    echoed = {
        "base_residency_mb": 15000.0,
        "max_context_length": 32768,
        "sync_revision": 4,
        "calibration_id": 7,
        "calibration_stale": False,
        "calibration_key_hash": "H1",
        "overridden_fields": ["max_context_length"],
    }
    assert persistable_profile(echoed) == {"base_residency_mb": 15000.0}


def test_reported_profile_keeps_overrides_but_drops_sync_metadata():
    echoed = {"max_context_length": 32768, "sync_revision": 1, "overridden_fields": ["max_context_length"]}
    assert reported_profile(echoed) == {"max_context_length": 32768}


def test_calibration_snapshot_keeps_only_calibration_fields():
    profile = {"base_residency_mb": 1.0, "host_ram_mb": 2.0, "calibration_unsupported": True}
    assert calibration_snapshot(profile) == {"base_residency_mb": 1.0}


def test_new_local_calibration_needs_key_and_no_snapshot_id():
    fresh = {
        "residency_source": "calibrated",
        "calibration_origin": "local",
        "calibration_key_hash": "H1",
        "last_measured_epoch": 1790000000.0,
    }
    assert is_new_local_calibration(fresh)
    assert not is_new_local_calibration({**fresh, "calibration_id": 3})
    assert not is_new_local_calibration({**fresh, "calibration_origin": "shared"})
    assert not is_new_local_calibration({**fresh, "calibration_key_hash": None})
    assert not is_new_local_calibration({**fresh, "residency_source": "measured"})
    assert not is_new_local_calibration({**fresh, "last_measured_epoch": 0})


def test_effective_profile_without_calibration_is_the_stored_record():
    profile = effective_profile(1, _row())
    assert profile == {"base_residency_mb": 15000.0, "residency_source": "calibrated", "sync_revision": 3}


def test_own_calibration_with_matching_key_is_local_and_fresh():
    profile = effective_profile(1, _row(cal_id=9, cal_key_hash="H1", cal_source_provider_id=1))
    assert profile["calibration_id"] == 9
    assert profile["calibration_origin"] == "local"
    assert profile["calibration_stale"] is False


def test_key_change_keeps_the_calibration_but_marks_it_stale():
    profile = effective_profile(
        1, _row(calibration_key_hash="H2", cal_id=9, cal_key_hash="H1", cal_source_provider_id=1)
    )
    assert profile["base_residency_mb"] == 15000.0
    assert profile["calibration_stale"] is True


def test_imported_calibration_is_legacy_and_stale():
    profile = effective_profile(1, _row(cal_id=9, cal_key_hash=None, cal_source_provider_id=1))
    assert profile["calibration_origin"] == "legacy"
    assert profile["calibration_stale"] is True


def test_calibration_from_another_node_is_shared():
    profile = effective_profile(1, _row(cal_id=9, cal_key_hash="H1", cal_source_provider_id=2))
    assert profile["calibration_origin"] == "shared"


def test_invalidated_calibration_adds_no_provenance():
    profile = effective_profile(1, _row(cal_id=9, cal_key_hash="H1", cal_invalidated_at="2026-09-30"))
    assert "calibration_id" not in profile


def test_base_residency_agreement_uses_five_percent():
    assert base_residency_agrees(15000.0, 15600.0) is True
    assert base_residency_agrees(15000.0, 16000.0) is False
    assert base_residency_agrees(None, 1.0) is None
    assert base_residency_agrees("0", 1.0) is None


def test_write_cache_skips_unchanged_digests_per_model():
    cache = ProfileWriteCache()
    digest = profile_digest(1, {"a": 1})
    assert not cache.unchanged(1, "m", digest)
    cache.remember(1, "m", digest)
    assert cache.unchanged(1, "m", digest)
    assert not cache.unchanged(1, "m", profile_digest(2, {"a": 1}))
    cache.remember(1, "other", digest)
    cache.forget(1, "m")
    assert not cache.unchanged(1, "m", digest)
    assert cache.unchanged(1, "other", digest)
    cache.forget(1)
    assert not cache.unchanged(1, "other", digest)


def test_material_profile_ignores_counters_and_float_jitter():
    one = {"loaded_vram_mb": 15100.31, "measurement_count": 4, "last_measured_epoch": 1.0, "engine": "vllm"}
    two = {"loaded_vram_mb": 15103.87, "measurement_count": 5, "last_measured_epoch": 2.0, "engine": "vllm"}
    assert material_profile(one) == material_profile(two) == {"loaded_vram_mb": 15100.0, "engine": "vllm"}
    assert material_profile({"loaded_vram_mb": 15300.0}) != material_profile(one)


def test_volatile_only_changes_wait_for_the_flush_interval():
    cache = ProfileWriteCache(flush_seconds=300.0)
    cache.remember(1, "m", "d1", "material", now=0.0)
    assert cache.unchanged(1, "m", "d1", now=10_000.0)
    assert cache.unchanged(1, "m", "d2", "material", now=299.0)
    assert not cache.unchanged(1, "m", "d2", "material", now=300.0)
    assert not cache.unchanged(1, "m", "d2", "other", now=1.0)
    assert not cache.unchanged(1, "m", "d2", now=1.0)


def test_a_write_overtaken_by_an_invalidation_is_not_remembered():
    cache = ProfileWriteCache()
    before = cache.generation(1, "m")
    cache.forget(1, "m")
    cache.remember(1, "m", "d", generation=before)
    assert not cache.unchanged(1, "m", "d")

    before = cache.generation(1, "m")
    cache.forget(1)
    cache.remember(1, "m", "d", generation=before)
    assert not cache.unchanged(1, "m", "d")

    cache.remember(1, "m", "d", generation=cache.generation(1, "m"))
    assert cache.unchanged(1, "m", "d")


def test_sharing_is_on_by_default_and_switchable(monkeypatch):
    monkeypatch.delenv("LOGOS_SHARED_PROFILES", raising=False)
    assert shared_profiles_enabled() is True
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv("LOGOS_SHARED_PROFILES", off)
        assert shared_profiles_enabled() is False
    monkeypatch.setenv("LOGOS_SHARED_PROFILES", "1")
    assert shared_profiles_enabled() is True


_MEASURED = {
    "base_residency_mb": 5000.0,
    "sleeping_residual_mb": 100.0,
    "sleep_l1_transient_host_ram_mb": 50.0,
    "residency_source": "calibrated",
    "kv_cache_to_max_model_len_pairs": [{"kv_mb": 1.0, "max_model_len": 2}],
}


def test_a_complete_calibration_needs_no_calibration():
    assert profile_needs_calibration(_MEASURED) is False


def test_every_missing_measurement_needs_calibration():
    assert profile_needs_calibration(None) is True
    assert profile_needs_calibration({**_MEASURED, "base_residency_mb": None}) is True
    assert profile_needs_calibration({**_MEASURED, "sleeping_residual_mb": None}) is True
    assert profile_needs_calibration({**_MEASURED, "kv_cache_to_max_model_len_pairs": None}) is True
    assert profile_needs_calibration({**_MEASURED, "calibration_stale": True}) is True
    assert profile_needs_calibration({**_MEASURED, "min_kv_cache_mb": 8.0, "max_kv_cache_mb": 8.0}) is True


def test_an_hf_estimate_still_needs_calibration():
    assert profile_needs_calibration({**_MEASURED, "residency_source": "hf"}) is True
    assert profile_needs_calibration({**_MEASURED, "residency_source": "hf", "calibration_unsupported": True}) is False


def test_a_model_that_cannot_sleep_is_judged_without_sleep_fields():
    profile = {**_MEASURED, "sleeping_residual_mb": None, "sleep_l1_transient_host_ram_mb": None}
    assert profile_needs_calibration({**profile, "sleep_mode_disabled": True}) is False


def test_an_unsupported_model_is_never_a_candidate():
    assert profile_needs_calibration({"calibration_unsupported": True}) is False
