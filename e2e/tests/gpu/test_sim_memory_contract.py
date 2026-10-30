"""Direct CPU regressions for the fake EngineCore memory contract.

These assert the below-worker boundary itself — not planner policy — so a soft
fake cannot greenwash an old cold-GMU / sleep-ledger bug. No Docker, no
orchestrator PYTHONPATH, no above-worker mocks.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from harness.gpusim import env as gpusim_env
from harness.gpusim import state as gpustate
from harness.gpusim.scenario import GpuScenario, VllmScript
from harness.gpusim.server import FAKE_VLLM_VERSION, VLLM_DEFAULT_GMU, assert_startup_gmu_free, assert_wake_capacity
from harness.gpusim.server import run as run_server
from harness.gpusim.server import simulation_assumptions, sleep_residual_mb_for

E2E_ROOT = Path(__file__).resolve().parents[2]
VLLM_SHIM = E2E_ROOT / "harness" / "gpusim" / "bin" / "vllm"
L40S_TOTAL = 46068.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(port: int, *, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    last: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=0.2)
            if response.status_code in (200, 503):
                return
        except Exception as exc:  # noqa: BLE001 - poll until up
            last = exc
        time.sleep(0.05)
    raise AssertionError(f"fake server on :{port} never became reachable: {last}")


def _server_process_main(
    model: str,
    port: int,
    script_json: str,
    vram_items: list[tuple[int, float]],
    startup_gmu: float,
    env: dict[str, str],
) -> None:
    """Top-level entry so multiprocessing 'spawn' can pickle the target."""
    os.environ.update(env)
    script = VllmScript.from_json(json.loads(script_json))
    code = run_server(
        model=model,
        port=port,
        script=script,
        vram_by_device={int(i): float(mb) for i, mb in vram_items},
        startup_gmu=startup_gmu,
    )
    raise SystemExit(code)


def _spawn_server(
    *,
    model: str,
    port: int,
    script: VllmScript,
    vram_by_device: dict[int, float],
    startup_gmu: float,
    env: dict[str, str],
) -> multiprocessing.Process:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_server_process_main,
        name="gpusim-memory-contract",
        args=(
            model,
            port,
            json.dumps(script.to_json()),
            sorted((int(i), float(mb)) for i, mb in vram_by_device.items()),
            float(startup_gmu),
            dict(env),
        ),
    )
    proc.start()
    return proc


@pytest.fixture
def sim_env(tmp_path, monkeypatch):
    """Two L40S cards with a clean ledger; PATH points at the shims."""
    built = gpusim_env.build(tmp_path / "gpusim", GpuScenario.homogeneous("l40s", 2))
    built.apply(monkeypatch)
    return built


def test_fake_vllm_version_is_simulated_0_32():
    assert FAKE_VLLM_VERSION == "0.32.0"
    assumptions = simulation_assumptions()
    assert assumptions["fake_vllm_version"] == "0.32.0"
    assert assumptions["fake_vllm_version_kind"] == "simulated_protocol"
    assert "0.03" in assumptions["sleep_l1_residual_note"] or "3.000%" in assumptions["sleep_l1_residual_note"]


def test_shim_version_imports_constant(sim_env, monkeypatch):
    monkeypatch.setenv("LOGOS_SIM_FAKE_VLLM_VERSION", "0.32.0")
    result = subprocess.run(
        [sys.executable, str(VLLM_SHIM), "--version"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **sim_env.env()},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "0.32.0"


def test_shim_publishes_cli_max_model_len_on_v1_models(sim_env):
    """Worker calibration reads /v1/models — must honor --max-model-len (not 32768)."""
    port = _free_port()
    env = {**os.environ, **sim_env.env()}
    proc = subprocess.Popen(
        [
            sys.executable,
            str(VLLM_SHIM),
            "serve",
            "Qwen/Qwen3.6-35B-A3B",
            "--port",
            str(port),
            "--max-model-len",
            "262144",
            "--tensor-parallel-size",
            "1",
            "--gpu-memory-utilization",
            "0.1",
            "--kv-cache-memory-bytes",
            "1048576",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_health(port, timeout_s=8.0)
        # Wait until ready (ready_after_s may be >0 from shared script).
        deadline = time.time() + 5.0
        payload = None
        while time.time() < deadline:
            response = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0)
            if response.status_code == 200:
                payload = response.json()
                break
            time.sleep(0.05)
        assert payload is not None, "never got /v1/models"
        models = payload.get("data") or []
        assert models, payload
        assert int(models[0].get("max_model_len")) == 262144, models[0]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_startup_gmu_gate_rejects_when_free_below_gmu_even_with_small_claim(sim_env):
    """Pinned-KV-style claim can be small; free still must cover gmu×total (B3)."""
    # Occupy almost the whole card so free << 0.5 × total, but request a tiny claim.
    with sim_env.mutate() as sim:
        sim.allocate(pid=1, device_index=0, mb=L40S_TOTAL - 1000.0)

    gmu = 0.5
    need = gmu * L40S_TOTAL
    assert sim_env.free_mb(0) < need

    with sim_env.mutate() as sim:
        with pytest.raises(gpustate.GpuSimStateError, match="Free memory"):
            assert_startup_gmu_free(sim, 0, gmu)


def test_startup_gmu_gate_passes_when_free_covers_reservation(sim_env):
    with sim_env.mutate() as sim:
        assert_startup_gmu_free(sim, 0, 0.5)
        assert_startup_gmu_free(sim, 1, VLLM_DEFAULT_GMU)


def test_server_startup_exits_nonzero_when_gmu_gate_fails(sim_env, monkeypatch):
    with sim_env.mutate() as sim:
        sim.allocate(pid=9, device_index=0, mb=L40S_TOTAL - 500.0)

    port = _free_port()
    env = {**os.environ, **sim_env.env()}
    # Tiny synthetic claim (KV-pin style) must still die on the GMU gate.
    proc = _spawn_server(
        model="org/tiny-kv",
        port=port,
        script=VllmScript(ready_after_s=0.0, emit_dev_mode_warning=False, vram_mb=256.0),
        vram_by_device={0: 256.0},
        startup_gmu=0.9,
        env=env,
    )
    proc.join(timeout=5.0)
    assert proc.exitcode == 1, f"expected GMU-gate failure exit, got {proc.exitcode}"
    assert sim_env.used_mb(0) == pytest.approx(L40S_TOTAL - 500.0), "failed start must not claim VRAM"


def test_server_startup_succeeds_with_enough_free_and_distinct_claim(sim_env):
    port = _free_port()
    claim = 4096.0
    proc = _spawn_server(
        model="org/ok",
        port=port,
        script=VllmScript(ready_after_s=0.0, emit_dev_mode_warning=False, vram_mb=claim),
        vram_by_device={0: claim},
        startup_gmu=0.5,
        env={**os.environ, **sim_env.env()},
    )
    try:
        _wait_health(port)
        assert sim_env.used_mb(0) == pytest.approx(claim)
        version = httpx.get(f"http://127.0.0.1:{port}/version", timeout=1.0).json()
        assert version["version"] == "0.32.0"
        assert version["simulated"] is True
    finally:
        proc.terminate()
        proc.join(timeout=5.0)


def test_sleep_shrinks_ledger_and_same_pid_wake_restores(sim_env):
    port = _free_port()
    awake = {0: 20_000.0, 1: 20_000.0}
    proc = _spawn_server(
        model="org/tp2",
        port=port,
        script=VllmScript(ready_after_s=0.0, emit_dev_mode_warning=False),
        vram_by_device=awake,
        startup_gmu=0.5,
        env={**os.environ, **sim_env.env()},
    )
    try:
        _wait_health(port)
        pid = proc.pid
        assert sim_env.used_mb(0) == pytest.approx(20_000.0)
        assert sim_env.used_mb(1) == pytest.approx(20_000.0)

        sleep = httpx.post(f"http://127.0.0.1:{port}/sleep?level=1", timeout=2.0)
        assert sleep.status_code == 200
        residual = sleep_residual_mb_for("org/tp2", 20_000.0)
        assert sim_env.used_mb(0) == pytest.approx(residual, rel=0.01)
        assert sim_env.used_mb(1) == pytest.approx(residual, rel=0.01)
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is True

        wake = httpx.post(f"http://127.0.0.1:{port}/wake_up", timeout=2.0)
        assert wake.status_code == 200
        assert sim_env.used_mb(0) == pytest.approx(20_000.0)
        assert sim_env.used_mb(1) == pytest.approx(20_000.0)
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is False
        # Same process (PID) still owns the restored claims.
        owners = {(a.pid, a.device_index, a.mb) for a in sim_env.state().allocations}
        assert (pid, 0, 20_000.0) in owners or any(
            a.pid == pid and a.device_index == 0 for a in sim_env.state().allocations
        )
    finally:
        proc.terminate()
        proc.join(timeout=5.0)


def test_wake_oom_preserves_sleep_flag_and_all_gpu_residuals(sim_env):
    port = _free_port()
    awake = {0: 30_000.0, 1: 30_000.0}
    proc = _spawn_server(
        model="org/tp2-oom",
        port=port,
        script=VllmScript(ready_after_s=0.0, emit_dev_mode_warning=False),
        vram_by_device=awake,
        startup_gmu=0.5,
        env={**os.environ, **sim_env.env()},
    )
    try:
        _wait_health(port)
        assert httpx.post(f"http://127.0.0.1:{port}/sleep?level=1", timeout=2.0).status_code == 200
        residual = sleep_residual_mb_for("org/tp2-oom", 30_000.0)
        before = {i: sim_env.used_mb(i) for i in (0, 1)}
        assert before[0] == pytest.approx(residual, rel=0.01)
        assert before[1] == pytest.approx(residual, rel=0.01)

        # Fill both cards so restoring 30 GiB awake claims cannot fit. Generic
        # allocate stays unbounded (fault injection); wake boundary must refuse.
        with sim_env.mutate() as sim:
            sim.allocate(pid=7001, device_index=0, mb=L40S_TOTAL - residual - 100.0)
            sim.allocate(pid=7002, device_index=1, mb=L40S_TOTAL - residual - 100.0)

        wake = httpx.post(f"http://127.0.0.1:{port}/wake_up", timeout=2.0)
        assert wake.status_code == 507, wake.text
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is True
        # Residual claims on *both* GPUs must be unchanged (no partial TP commit).
        assert sim_env.used_mb(0) == pytest.approx(before[0] + (L40S_TOTAL - residual - 100.0), rel=0.01)
        assert sim_env.used_mb(1) == pytest.approx(before[1] + (L40S_TOTAL - residual - 100.0), rel=0.01)
        lane_claims = [a for a in sim_env.state().allocations if a.pid == proc.pid]
        assert len(lane_claims) == 2
        assert all(a.mb == pytest.approx(residual, rel=0.01) for a in lane_claims)
    finally:
        proc.terminate()
        proc.join(timeout=5.0)


def test_corrupt_state_sleep_and_wake_return_http_error_without_flipping_flag(sim_env):
    port = _free_port()
    proc = _spawn_server(
        model="org/state",
        port=port,
        script=VllmScript(ready_after_s=0.0, emit_dev_mode_warning=False),
        vram_by_device={0: 8_000.0},
        startup_gmu=0.5,
        env={**os.environ, **sim_env.env()},
    )
    try:
        _wait_health(port)
        # Corrupt the ledger so mutate/load raises GpuSimStateError.
        sim_env.state_file.write_text("{not-json", encoding="utf-8")

        sleep = httpx.post(f"http://127.0.0.1:{port}/sleep?level=1", timeout=2.0)
        assert sleep.status_code == 500, sleep.text
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is False

        # Restore a valid sleeping ledger, sleep for real, then corrupt again for wake.
        gpustate.save(
            GpuScenario.homogeneous("l40s", 2).to_state(),
            sim_env.state_file,
        )
        with sim_env.mutate() as sim:
            sim.allocate(pid=proc.pid, device_index=0, mb=8_000.0)
        assert httpx.post(f"http://127.0.0.1:{port}/sleep?level=1", timeout=2.0).status_code == 200
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is True

        sim_env.state_file.write_text("{still-broken", encoding="utf-8")
        wake = httpx.post(f"http://127.0.0.1:{port}/wake_up", timeout=2.0)
        assert wake.status_code in (500, 507), wake.text
        # Missing/corrupt state must not report awake.
        # /is_sleeping only reads in-process flag — should still be True.
        assert httpx.get(f"http://127.0.0.1:{port}/is_sleeping", timeout=1.0).json()["is_sleeping"] is True
    finally:
        proc.terminate()
        proc.join(timeout=5.0)


def test_missing_state_file_wake_capacity_helper_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(gpustate.STATE_ENV_VAR, raising=False)
    monkeypatch.setenv(gpustate.STATE_ENV_VAR, str(tmp_path / "missing.json"))
    with pytest.raises(gpustate.GpuSimStateError):
        with gpustate.mutate() as sim:
            assert_wake_capacity(sim, pid=1, awake_by_device={0: 100.0})


def test_per_model_residual_mb_override(monkeypatch):
    monkeypatch.setenv(
        "LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON",
        json.dumps({"org/calibrated": 1024.0}),
    )
    assert sleep_residual_mb_for("org/calibrated", 40_000.0) == pytest.approx(1024.0)
    # Unlisted models keep the fractional approximation.
    assert sleep_residual_mb_for("org/other", 10_000.0) == pytest.approx(300.0)
    assumptions = simulation_assumptions()
    assert assumptions["sleep_l1_residual_mode"] == "per_model_mb"
    assert assumptions["sleep_l1_residual_mb_by_model"]["org/calibrated"] == 1024.0


def test_oom_on_start_script_still_exits(sim_env):
    port = _free_port()
    proc = _spawn_server(
        model="org/forced-oom",
        port=port,
        script=VllmScript(oom_on_start=True, emit_dev_mode_warning=False),
        vram_by_device={0: 1000.0},
        startup_gmu=0.1,
        env={**os.environ, **sim_env.env()},
    )
    proc.join(timeout=5.0)
    assert proc.exitcode == 1
    assert sim_env.used_mb(0) == 0.0


def test_generic_allocate_remains_unbounded_for_fault_injection(sim_env):
    with sim_env.mutate() as sim:
        sim.allocate(pid=4242, device_index=0, mb=L40S_TOTAL * 2)
    assert sim_env.used_mb(0) > L40S_TOTAL
