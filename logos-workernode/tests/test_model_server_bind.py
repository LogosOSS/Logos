"""Model servers expose their unauthenticated APIs only on IPv4 loopback."""

from __future__ import annotations

import argparse
from unittest.mock import Mock

import bench_vllm_sleep
import pytest

from logos_worker_node.calibration import _build_vllm_cmd
from logos_worker_node.calibration_metal import _build_metal_calibration_cmd
from logos_worker_node.metal_process import MetalVllmProcessHandle
from logos_worker_node.models import LaneConfig, VllmConfig, VllmEngineConfig, WorkerConfig
from logos_worker_node.vllm_process import VllmProcessHandle

HOST_OVERRIDES = [
    ["--host", "0.0.0.0"],
    ["--host=0.0.0.0"],
    ["-host", "::"],
    ["-host=192.0.2.1"],
    ["--hos", "0.0.0.0"],
    ["--ho=0.0.0.0"],
    ["-ho", "0.0.0.0"],
    ["--host", "0.0.0.0", "-host=::"],
    ["--host"],
]
SERVERS = ["cuda-lane", "cuda-global", "metal-lane", "metal-global", "cuda-probe", "metal-probe"]


def build_command(server, extras, monkeypatch):
    plan = {"model": "org/model", "extra_args": extras, "host": "0.0.0.0"}
    if server == "cuda-probe":
        monkeypatch.setattr("logos_worker_node.calibration.probe_attention_backend", lambda _plan: "")
        return _build_vllm_cmd(plan, "vllm", "127.0.0.1", 11499, "1G")
    if server == "metal-probe":
        return _build_metal_calibration_cmd(plan, ["vllm"], "127.0.0.1", 11499)

    handle_class = MetalVllmProcessHandle if server.startswith("metal") else VllmProcessHandle
    global_args = server.endswith("global")
    handle = handle_class(
        "lane-test",
        11436,
        WorkerConfig(),
        VllmEngineConfig(global_extra_args=extras if global_args else []),
    )
    monkeypatch.setattr(handle, "_resolve_vllm_binary", lambda _binary: ["vllm"])
    monkeypatch.setattr("logos_worker_node.vllm_process.detect_cuda_arch", lambda: "")
    lane = LaneConfig(
        model="org/model",
        vllm=True,
        vllm_config=VllmConfig(
            extra_args=[] if global_args else extras,
            env_overrides={"HOST": "0.0.0.0", "UVICORN_HOST": "0.0.0.0", "VLLM_HOST_IP": "192.0.2.1"},
        ),
    )
    return handle._build_cmd(lane)


def assert_loopback(cmd):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", action="append")
    args, _ = parser.parse_known_args(cmd[3:])
    assert args.host
    assert set(args.host) == {"127.0.0.1"}
    assert "0.0.0.0" not in cmd
    assert "::" not in cmd


@pytest.mark.parametrize("server", SERVERS)
def test_model_server_binds_loopback_with_host_environment(server, monkeypatch):
    for name in ("HOST", "UVICORN_HOST", "VLLM_HOST_IP"):
        monkeypatch.setenv(name, "0.0.0.0")
    assert_loopback(build_command(server, [], monkeypatch))


@pytest.mark.parametrize("server", SERVERS)
@pytest.mark.parametrize("override", HOST_OVERRIDES)
def test_model_server_strips_host_overrides(server, override, monkeypatch, caplog):
    extras = ["--seed", "42", *override, "--trust-remote-code"]
    original = extras.copy()
    cmd = build_command(server, extras, monkeypatch)
    assert_loopback(cmd)
    assert not any(arg.startswith(("-host", "--host=", "--hos=", "--ho", "-ho")) for arg in cmd if arg != "--host")
    assert cmd[cmd.index("--seed") + 1] == "42"
    assert "--trust-remote-code" in cmd
    assert extras == original
    assert "Ignoring" in caplog.text
    assert "model servers must bind to 127.0.0.1" in caplog.text


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.1", "localhost"])
@pytest.mark.parametrize("server", ["cuda-probe", "metal-probe", "benchmark"])
def test_model_server_rejects_non_loopback_host(server, host, tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr("subprocess.Popen", popen)
    with pytest.raises(ValueError, match="must bind to 127.0.0.1"):
        if server == "cuda-probe":
            _build_vllm_cmd({"model": "org/model"}, "vllm", host, 11499, "1G")
        elif server == "metal-probe":
            _build_metal_calibration_cmd({"model": "org/model"}, ["vllm"], host, 11499)
        else:
            bench_vllm_sleep.start_vllm({"name": "org/model"}, "vllm", host, 11560, tmp_path / "probe.log")
    popen.assert_not_called()


def test_benchmark_spawns_on_loopback(tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(bench_vllm_sleep.subprocess, "Popen", popen)
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    _, cmd = bench_vllm_sleep.start_vllm({"name": "org/model"}, "vllm", "127.0.0.1", 11560, tmp_path / "probe.log")
    assert_loopback(cmd)
    assert popen.call_args.args[0] == cmd
