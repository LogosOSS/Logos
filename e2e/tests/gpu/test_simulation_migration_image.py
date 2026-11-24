"""Exercise retry behavior and the image-only Compose wiring without pulling images."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from harness.stack import compose

_BUILD = Path(__file__).resolve().parents[3] / "benchmarks/residency_simulation/build_migration_image.sh"


@pytest.mark.parametrize(
    ("outcomes", "returncode", "delays"),
    [
        (["429 Too Many Requests", "RETRY_OK"], 0, ["10"]),
        (["Could not transfer artifact: status code: 503"] * 3, 24, ["10", "20"]),
        (["COMPILATION ERROR", "RETRY_OK"], 24, []),
        (["Could not transfer artifact: status code: 404"], 24, []),
    ],
)
def test_image_build_retries_transient_downloads_only(tmp_path, outcomes, returncode, delays):
    docker = tmp_path / "docker"
    docker.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$TRACE/commands"
next="$(wc -l < "$TRACE/commands" | tr -d ' ')"
outcome="$(sed -n "${next}p" "$TRACE/outcomes")"
if [[ "$outcome" != RETRY_OK ]]; then echo "$outcome" >&2; exit 24; fi
""")
    sleep = tmp_path / "sleep"
    sleep.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$1" >> "$TRACE/delays"\n')
    for tool in (docker, sleep):
        tool.chmod(0o755)
    (tmp_path / "outcomes").write_text("\n".join(outcomes) + "\n")
    (tmp_path / "delays").touch()
    result = subprocess.run(
        ["bash", str(_BUILD)],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "TRACE": str(tmp_path),
            "E2E_SIM_MIGRATE_IMAGE": "logos-sim-migrate:123",
            "E2E_SIM_SOURCE_SHA": "revision",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == returncode
    assert (tmp_path / "delays").read_text().splitlines() == delays
    commands = (tmp_path / "commands").read_text().splitlines()
    assert len(commands) == len(delays) + 1
    assert set(commands) == {
        "buildx build --load --progress=plain --target e2e-migrate --tag logos-sim-migrate:123 "
        "--label org.opencontainers.image.revision=revision logos-webservice"
    }


def test_only_simulation_benchmark_stacks_reuse_preloaded_image(monkeypatch):
    monkeypatch.delenv("E2E_PERFORMANCE_OVERLAY", raising=False)
    monkeypatch.delenv("E2E_SIM_MIGRATE_IMAGE", raising=False)
    with patch.object(compose.subprocess, "run") as runner:
        compose._compose("up", "--build", benchmark_topology=True)
        assert str(compose.SIMULATION_MIGRATION_FILE) not in runner.call_args.args[0]
        monkeypatch.setenv("E2E_SIM_MIGRATE_IMAGE", "logos-sim-migrate:123")
        compose._compose("up", "--build", benchmark_topology=False)
        assert str(compose.SIMULATION_MIGRATION_FILE) not in runner.call_args.args[0]
        compose._compose("up", "--build", benchmark_topology=True)
        assert str(compose.SIMULATION_MIGRATION_FILE) in runner.call_args.args[0]
        monkeypatch.setenv("E2E_PERFORMANCE_OVERLAY", "/perf.yaml")
        compose._compose("up", "--no-build", benchmark_topology=True)
        cmd = runner.call_args.args[0]
        assert cmd.index(str(compose.SIMULATION_MIGRATION_FILE)) > cmd.index("/perf.yaml")
