"""Small profile and GPU-slice helpers shared by calibration and the registry.

Kept apart from ``calibration`` so the profile registry and the fingerprint
can use them without importing the calibration driver.
"""

from __future__ import annotations

from typing import Any


def parse_gpu_indices(gpu_devices: str) -> list[int] | None:
    """``'0,1'`` → ``[0, 1]``; ``''`` or ``'all'`` → ``None`` (all GPUs)."""
    gd = (gpu_devices or "").strip().lower()
    if not gd or gd == "all":
        return None
    return [int(x.strip()) for x in gd.split(",") if x.strip().isdigit()]


def calibration_gpu_slice(available_gpus: int) -> list[int]:
    """Return the GPU indices a calibration run should be pinned to.

    The largest power-of-two slice of the node's GPUs, starting at index 0
    (3 GPUs → ``[0, 1]``, 5 → ``[0, 1, 2, 3]``, 8 → ``[0..7]``). The slice
    length equals the maximum tensor-parallel size the node supports, so the
    existing max-first TP escalation fits inside it unchanged.

    Pinning the calibration to this concrete slice leaves the
    leftover GPUs (``slice_size..N-1``) free for production lanes, which would
    otherwise sit idle for the entire calibration window, and makes the VRAM
    baseline a well-defined sum over exactly the GPUs the probe uses.
    """
    n = int(available_gpus) if available_gpus else 0
    if n < 1:
        return []
    slice_size = 1 << (n.bit_length() - 1)
    return list(range(slice_size))


# Fields the probe owns outright: it either measures them or states that they
# do not apply, and both answers are authoritative. Everything else it leaves
# ``None`` simply because it did not look — a flag maintained elsewhere, an
# operator override, or a sleep level this run did not exercise — and for
# those ``None`` must not erase what is already known.
#
# ``sleeping_residual_mb`` is on this list because a run reports it null
# exactly when the model must not be slept here: the sleep phases were
# skipped (sleep disabled) or they ran but failed verification
# (sleep_mode_disabled=True). Keeping a stale measurement then hides that. It
# also survives an ``enable_sleep_mode`` flip back to true, where the freshness
# check sees a value, declines to re-calibrate, and hands the planner a
# residual measured under a configuration that no longer exists.
_PROBE_OWNED_PROFILE_FIELDS = frozenset({"sleeping_residual_mb"})


def merge_profile(prior: dict[str, Any] | None, measured: dict[str, Any]) -> dict[str, Any]:
    """Layer a fresh calibration result over the profile already on disk.

    A calibration run measures what it can reach and leaves the rest ``None``:
    the sleep host-RAM transients for a level it did not run, and every field
    the probe does not look at at all (``disk_size_bytes``,
    ``sleep_mode_disabled``, ``calibration_unsupported``, the operator's
    overrides). Assigning the result over the entry wipes those — the flags
    included, so a nosleep model loses the very marker that says its null
    sleep fields are expected. A measured value replaces the stored one;
    ``None`` means "not measured here" and keeps what was already known,
    except for the fields in :data:`_PROBE_OWNED_PROFILE_FIELDS`, where a null
    is itself the measurement.
    """
    merged = dict(prior or {})
    for key, value in measured.items():
        if value is None and key in merged and key not in _PROBE_OWNED_PROFILE_FIELDS:
            continue
        merged[key] = value
    return merged
