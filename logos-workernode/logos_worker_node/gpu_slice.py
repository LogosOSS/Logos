"""GPU index helpers shared by calibration, the fingerprint and the bridge.

Kept apart from ``calibration`` so the fingerprint can use them without
importing the calibration driver.
"""

from __future__ import annotations


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
