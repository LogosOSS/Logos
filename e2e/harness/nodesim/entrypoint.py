"""Container entrypoint for a simulated worker node.

Materialises the GPU scenario this node was configured with, then hands over to
the real ``logos_worker_node`` process. Nothing about the worker is patched —
the only difference from production is which binaries are first on ``PATH``.

Configured entirely through the environment so a compose file can describe a
heterogeneous fleet without a config file per node:

``LOGOS_SIM_PROFILE``       GPU profile key, or a comma-separated list for a
                            mixed-architecture node (default ``l40s``)
``LOGOS_SIM_GPU_COUNT``     number of cards, when a single profile is named
``LOGOS_SIM_BASELINE_MB``   VRAM already in use before any lane starts
``LOGOS_URL``               orchestrator base URL
``LOGOS_ADMIN_KEY``         admin key used to self-register when ``LOGOS_API_KEY``
                            is not supplied (default: the seeded E2E admin key)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, "/app")

from harness.gpusim import state as gpustate  # noqa: E402
from harness.gpusim.scenario import GpuScenario, VllmScript  # noqa: E402

DEFAULT_ADMIN_KEY = "lg-e2e-admin-key"
REGISTER_PATH = "/logosdb/providers/logosnode/register"


def build_scenario() -> GpuScenario:
    raw_profiles = os.environ.get("LOGOS_SIM_PROFILE", "l40s").strip()
    profiles = [p.strip() for p in raw_profiles.split(",") if p.strip()]
    if len(profiles) == 1:
        count = int(os.environ.get("LOGOS_SIM_GPU_COUNT", "1"))
        profiles = profiles * count
    return GpuScenario(
        profiles=profiles,
        baseline_used_mb=float(os.environ.get("LOGOS_SIM_BASELINE_MB", "0") or 0),
    )


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def self_register(logos_url: str, provider_name: str, deadline_s: float = 120.0) -> str:
    """Register this node and return its shared key.

    Deliberately the real endpoint rather than a pre-seeded provider row: node
    registration is one of the things the inter-node tier is here to cover, and
    a fixture that skips it would let a regression in it reach production.

    Retries because the orchestrator's healthcheck passing does not yet mean its
    database session pool is warm.
    """
    admin_key = os.environ.get("LOGOS_ADMIN_KEY", DEFAULT_ADMIN_KEY)
    # Required by the endpoint — there is no default, so every caller states the
    # trust level explicitly. Overridable so a scenario can register a node as
    # third-party hardware and assert the routing restrictions that follow.
    privacy_level = os.environ.get("LOGOS_SIM_PRIVACY_LEVEL", "LOCAL")
    url = logos_url.rstrip("/") + REGISTER_PATH
    payload = {
        "logos_key": admin_key,
        "provider_name": provider_name,
        "base_url": "",
        "privacy_level": privacy_level,
    }

    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = _post_json(url, payload)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            last_error = exc
            time.sleep(2.0)
            continue
        shared_key = result.get("shared_key")
        if shared_key:
            print(f"[nodesim] registered as provider {result.get('provider_id')} ({provider_name})", flush=True)
            return shared_key
        last_error = RuntimeError(f"register returned no shared_key: {result}")
        time.sleep(2.0)

    raise RuntimeError(f"[nodesim] could not register with {url} within {deadline_s:.0f}s: {last_error}")


def _capabilities_yaml_lines() -> list[str]:
    """Optional benchmark model catalogue for the worker config.

    ``LOGOS_SIM_CAPABILITIES=benchmark5`` expands to the five pinned model
    names only. Calibration residency stays in central ``model_profiles``;
    frozen engine pins go under ``engines.vllm.model_overrides`` (separate).
    """
    raw = os.environ.get("LOGOS_SIM_CAPABILITIES", "").strip()
    if not raw:
        return []
    if raw == "benchmark5":
        from harness.benchmark_topology import BENCHMARK_MODELS_5  # noqa: PLC0415

        lines = ["  capabilities_models:"]
        for model in BENCHMARK_MODELS_5:
            lines.append(f"    - {model!r}")
        return lines
    models = [m.strip() for m in raw.split(",") if m.strip()]
    if not models:
        return []
    lines = ["  capabilities_models:"]
    for model in models:
        lines.append(f"    - {model!r}")
    return lines


def _yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if value is None:
        return "null"
    return repr(str(value))


def _engine_overrides_yaml_lines() -> list[str]:
    """Emit frozen engine pins for cold-GMU / KV-pin admission (c936+).

    Loaded from public ``benchmark_engine_specs_public.json`` (or
    ``E2E_ENGINE_SPECS_FIXTURE``). Calibration ``kv_budget_mb`` in model_profiles
    stays separate — Gemma-4b calib ≈30 GiB vs engine pin ≈13 GiB.
    """
    if os.environ.get("LOGOS_SIM_CAPABILITIES", "").strip() != "benchmark5":
        return []
    from harness.benchmark_topology import BENCHMARK_MODELS_5, engine_vllm_overrides, load_engine_specs  # noqa: PLC0415

    load_engine_specs()
    lines = [
        "engines:",
        "  vllm:",
        "    model_overrides:",
    ]
    for model in BENCHMARK_MODELS_5:
        overrides = engine_vllm_overrides(model)
        if not overrides:
            continue
        lines.append(f"      {model!r}:")
        for key, value in overrides.items():
            if isinstance(value, dict):
                lines.append(f"        {key}:")
                for nested_k, nested_v in value.items():
                    lines.append(f"          {nested_k}: {_yaml_scalar(nested_v)}")
            else:
                lines.append(f"        {key}: {_yaml_scalar(value)}")
    return lines if len(lines) > 3 else []


def write_config(path: Path) -> None:
    """Minimal worker config. Credentials stay in the environment, as in production."""
    prefetch = os.environ.get("LOGOS_PREFETCH_MISSING_MODELS", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    name = os.environ.get("LOGOS_SIM_NODE_NAME") or os.environ.get("HOSTNAME", "nodesim")
    lines = [
        "worker:",
        "  port: 80",
        f"  name: {name}",
        "  gpu_poll_interval: 2",
        "  models_path: /app/models",
        "  cache_path: /app/cache",
        "  auto_reboot_on_stuck_gpu: false",
        f"  prefetch_missing_models: {'true' if prefetch else 'false'}",
        "logos:",
        "  heartbeat_interval_seconds: 2",
        "  reconnect_backoff_seconds: 1",
        "  status_refresh_interval_seconds: 2",
    ]
    lines.extend(_capabilities_yaml_lines())
    lines.extend(_engine_overrides_yaml_lines())
    lines.extend(["lanes: []", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    state_file = Path(os.environ[gpustate.STATE_ENV_VAR])
    state_file.parent.mkdir(parents=True, exist_ok=True)
    gpustate.save(build_scenario().to_state(), state_file)

    script_file = Path(os.environ["LOGOS_GPUSIM_VLLM_SCRIPT"])
    if not script_file.is_file():
        ready_after = float(os.environ.get("LOGOS_SIM_VLLM_READY_AFTER_S", "0.2") or 0.2)
        VllmScript(ready_after_s=ready_after).write(script_file)

    state = gpustate.load(state_file)
    print(
        f"[nodesim] simulating {len(state.devices)} device(s): "
        f"{', '.join(d.fields.get('name', '?') for d in state.devices)}",
        flush=True,
    )

    write_config(Path(os.environ["LOGOS_WORKER_NODE_CONFIG"]))
    _install_sleep_residual_map(state_dir=Path(os.environ.get("LOGOS_STATE_DIR", "/app/data")))

    logos_url = os.environ.get("LOGOS_URL", "").strip()
    # Persist the shared key across container restarts so the provider_id stays
    # stable. Re-registering every restart orphans calibrated profiles and
    # leaves the worker advertising empty capabilities.
    state_dir = Path(os.environ.get("LOGOS_STATE_DIR", "/app/data"))
    state_dir.mkdir(parents=True, exist_ok=True)
    key_file = state_dir / "shared_key"
    if not os.environ.get("LOGOS_API_KEY", "").strip():
        if key_file.is_file():
            saved = key_file.read_text(encoding="utf-8").strip()
            if saved:
                os.environ["LOGOS_API_KEY"] = saved
                print(f"[nodesim] reused persisted shared key from {key_file}", flush=True)
        if not os.environ.get("LOGOS_API_KEY", "").strip() and logos_url:
            provider_name = os.environ.get("LOGOS_SIM_NODE_NAME") or os.environ.get("HOSTNAME", "nodesim")
            os.environ["LOGOS_API_KEY"] = self_register(logos_url, provider_name)
            key_file.write_text(os.environ["LOGOS_API_KEY"], encoding="utf-8")
            print(f"[nodesim] persisted shared key to {key_file}", flush=True)

    _install_host_ram_simulation()

    from logos_worker_node.main import main as worker_main  # noqa: PLC0415

    worker_main()


def _install_sleep_residual_map(*, state_dir: Path) -> None:
    """Point gpusim at calibrated per-device residuals from the public fixture.

    Skipped when the operator already set ``LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON``
    or ``_FILE``. Divides profile ``sleeping_residual_mb`` by TP so each card's
    ledger matches production-scale residuals instead of the 3% synthetic default.
    """
    if os.environ.get("LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_JSON", "").strip():
        return
    if os.environ.get("LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_FILE", "").strip():
        return
    if os.environ.get("LOGOS_SIM_CAPABILITIES", "").strip() != "benchmark5":
        return
    from harness.benchmark_topology import sleep_l1_residual_mb_by_model  # noqa: PLC0415

    residuals = sleep_l1_residual_mb_by_model()
    if not residuals:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "sleep_l1_residual_mb.json"
    path.write_text(json.dumps(residuals, indent=2) + "\n", encoding="utf-8")
    os.environ["LOGOS_SIM_SLEEP_L1_RESIDUAL_MB_FILE"] = str(path)
    print(
        f"[nodesim] sleep_L1 residual map ({len(residuals)} models) -> {path}",
        flush=True,
    )


def _install_host_ram_simulation() -> None:
    """Report production-sized host RAM to the planner (Docker Desktop is ~8 GB).

    Without this, every ``sleep_l1`` escalates to ``stop`` because the planner
    requires ≥8192 MB free host headroom. Simulated VRAM stays honest; only the
    host-RAM telemetry layer below the worker is faked.
    """
    raw = os.environ.get("LOGOS_SIM_HOST_RAM_AVAILABLE_MB", "").strip()
    if not raw:
        return
    available_mb = float(raw)
    total_mb = float(os.environ.get("LOGOS_SIM_HOST_RAM_TOTAL_MB", str(available_mb * 2)))
    from datetime import datetime, timezone  # noqa: PLC0415

    import logos_worker_node.runtime as runtime  # noqa: PLC0415
    from logos_worker_node.models import HostMemorySummary  # noqa: PLC0415

    def _fake_summary() -> HostMemorySummary:
        return HostMemorySummary(
            timestamp=datetime.now(timezone.utc),
            source="proc-meminfo",
            total_mb=total_mb,
            available_mb=available_mb,
            used_mb=max(total_mb - available_mb, 0.0),
            swap_total_mb=0.0,
            swap_used_mb=0.0,
        )

    runtime._build_host_memory_summary = _fake_summary  # type: ignore[assignment]
    print(
        f"[nodesim] simulating host RAM available={available_mb:.0f} MB total={total_mb:.0f} MB",
        flush=True,
    )


if __name__ == "__main__":
    main()
