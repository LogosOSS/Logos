"""Post-up bootstrap for the dual-Ada benchmark topology.

Seeds models + team permissions + calibrated profiles for every connected
logosnode provider, then restarts the Ada workers so they re-fetch central
profiles. Invoked by operators/CI after ``compose up --benchmark-topology`` —
tests never own stack lifecycle.

``base_residency_mb`` already includes KV; ``loaded_vram_mb`` is written as-is
(never base+kv). Optional ``--profile-fixture`` / ``E2E_PROFILE_FIXTURE``.

Seeds only at bootstrap — never rewrite live ``model_profiles`` mid-traffic to
force green; worker/fake-layer observations remain authoritative after seed.
Frozen engine pins are worker ``engines.vllm.model_overrides`` (entrypoint),
not rows in this seed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from harness.benchmark_topology import (
    BENCHMARK_MODEL_PROFILES,
    BENCHMARK_MODELS_5,
    BENCHMARK_NODE_SERVICES,
    load_profile_fixture,
)
from harness.clients.admin import AdminClient
from harness.stack import compose


def _psql(sql: str) -> None:
    result = compose._compose(
        "exec",
        "-T",
        "logos-db",
        "psql",
        "-U",
        "postgres",
        "-d",
        "logosdb",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        check=False,
        capture=True,
        benchmark_topology=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql failed:\n{result.stdout}\n{result.stderr}")


def seed_team_rate_limits() -> None:
    """Raise e2e team RPM/TPM so burst patterns are not 429'd at 5/60s."""
    _psql("""
        UPDATE teams
        SET default_local_rpm_limit = 100000,
            default_local_tpm_limit = 100000000,
            default_cloud_rpm_limit = 100000,
            default_cloud_tpm_limit = 100000000
        WHERE name = 'e2e';
        """)


def seed_models_and_permissions() -> None:
    seed_team_rate_limits()
    for model in BENCHMARK_MODELS_5:
        _psql(f"""
            INSERT INTO models (name, weight_latency, weight_accuracy, weight_cost, weight_quality, description)
            SELECT '{model}', 0, 0, 0, 0, 'e2e synthetic profile binding'
            WHERE NOT EXISTS (SELECT 1 FROM models WHERE name = '{model}');
            INSERT INTO team_model_permissions (team_id, model_id)
            SELECT t.id, m.id FROM teams t, models m
            WHERE t.name = 'e2e' AND m.name = '{model}'
              AND NOT EXISTS (
                SELECT 1 FROM team_model_permissions tmp
                WHERE tmp.team_id = t.id AND tmp.model_id = m.id
              );
            """)


def seed_provider_links(provider_ids: list[int]) -> None:
    """Grant the e2e team every Ada provider and link all five models.

    Deployments for ``/v1/chat/completions`` require ``model_provider`` rows
    plus ``team_provider_permissions``. Worker capability sync normally creates
    the former; restarts that mint new provider ids leave the team blind unless
    we repair both tables here.
    """
    for provider_id in provider_ids:
        _psql(f"""
            INSERT INTO team_provider_permissions (team_id, provider_id)
            SELECT t.id, {int(provider_id)} FROM teams t
            WHERE t.name = 'e2e'
              AND NOT EXISTS (
                SELECT 1 FROM team_provider_permissions tpp
                WHERE tpp.team_id = t.id AND tpp.provider_id = {int(provider_id)}
              );
            """)
        for model in BENCHMARK_MODELS_5:
            _psql(f"""
                INSERT INTO model_provider (provider_id, model_id)
                SELECT {int(provider_id)}, m.id FROM models m
                WHERE m.name = '{model}'
                  AND NOT EXISTS (
                    SELECT 1 FROM model_provider mp
                    WHERE mp.provider_id = {int(provider_id)} AND mp.model_id = m.id
                  );
                """)


def restrict_model_to_providers(model: str, provider_ids: list[int]) -> None:
    """Limit ``model_provider`` rows for *model* to the given provider ids.

    Uses the real catalogue table the scheduler reads — not a mock. This alone
    does **not** remove preexisting worker lanes; pair with
    :func:`prepare_lifecycle_isolation` / ``AdminClient.delete_lane`` when a
    clean exclusive placement is required.
    """
    if not provider_ids:
        raise ValueError("restrict_model_to_providers requires at least one provider_id")
    keep = ", ".join(str(int(p)) for p in provider_ids)
    _psql(f"""
        DELETE FROM model_provider
        WHERE model_id = (SELECT id FROM models WHERE name = '{model}')
          AND provider_id NOT IN ({keep});
        INSERT INTO model_provider (provider_id, model_id)
        SELECT p.provider_id, m.id
        FROM (VALUES {", ".join(f"({int(p)})" for p in provider_ids)}) AS p(provider_id)
        CROSS JOIN models m
        WHERE m.name = '{model}'
          AND NOT EXISTS (
            SELECT 1 FROM model_provider mp
            WHERE mp.provider_id = p.provider_id AND mp.model_id = m.id
          );
        """)


def delete_model_lanes_on_providers(
    admin: AdminClient,
    model: str,
    provider_ids: list[int],
    *,
    timeout_s: float = 60.0,
    poll_s: float = 1.0,
) -> list[tuple[int, str]]:
    """Delete every live lane for *model* on the given providers via management API.

    Returns ``(provider_id, lane_id)`` pairs that were requested for deletion.
    Polls until those models are gone (or *timeout_s*). Does not forge telemetry
    or rewrite ``model_profiles``.
    """
    deleted: list[tuple[int, str]] = []
    for provider_id in provider_ids:
        for lane in admin.node_lanes(int(provider_id)):
            if lane.get("model") != model:
                continue
            lane_id = str(lane.get("lane_id") or "")
            if not lane_id:
                continue
            admin.delete_lane(int(provider_id), lane_id)
            deleted.append((int(provider_id), lane_id))

    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        remaining = []
        for provider_id in provider_ids:
            for lane in admin.node_lanes(int(provider_id)):
                if lane.get("model") == model:
                    remaining.append((int(provider_id), str(lane.get("lane_id"))))
        if not remaining:
            return deleted
        time.sleep(float(poll_s))
    raise TimeoutError(
        f"lanes for {model!r} still present on {provider_ids} after delete "
        f"(timeout_s={timeout_s}); last={remaining}"
    )


def prepare_lifecycle_isolation(
    admin: AdminClient,
    *,
    target_model: str,
    keep_provider_id: int,
    also_restrict: list[str] | None = None,
    all_provider_ids: list[int] | None = None,
    refresh_orchestrator_cache: bool = True,
) -> dict[str, object]:
    """Deterministic lifecycle SETUP before exclusive pin / capacity pressure.

    Honest sequence (documented for reports):

    1. Restrict ``model_provider`` for *target_model* (and optional pressure
       models) to *keep_provider_id* only — catalogue cannot answer elsewhere.
    2. Optionally restart the orchestrator so in-memory deployment maps drop
       stale provider bindings (SQL alone may leave a cached map).
    3. Delete preexisting target engines on every *other* provider through the
       real ``/logosdb/providers/logosnode/lanes/delete`` API. Catalogue
       removal never unloads a running/sleeping lane; idle-settle is unreliable
       under retain-last-active.

    When ``refresh_orchestrator_cache=True`` this performs a compose restart —
    call only from the external runner / ``--prepare-lifecycle`` CLI, never from
    pytest (see ``e2e/AGENTS.md``).

    Returns a small facts dict for logging (not invented metrics).
    """
    keep = int(keep_provider_id)
    if all_provider_ids is None:
        all_provider_ids = [int(n.provider_id) for n in admin.connected_nodes()]
    others = [int(p) for p in all_provider_ids if int(p) != keep]
    models = [target_model, *(also_restrict or [])]
    for model in models:
        restrict_model_to_providers(model, [keep])

    deleted: list[tuple[int, str]] = []
    # Delete *before* orch restart while worker sessions still advertise lanes.
    # A restart briefly yields empty lane lists; delete-then would no-op and
    # foreign engines reappear after websocket reconnect (seen under LRU).
    for model in models:
        if others:
            deleted.extend(delete_model_lanes_on_providers(admin, model, others))

    orch_restarted = False
    if refresh_orchestrator_cache:
        restart_orchestrator()
        # Workers re-bind websocket after orch restart; lane deletes need a live session.
        compose._ACTIVE_BENCHMARK_TOPOLOGY = True
        compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES
        compose.wait_for_nodes(expected=max(len(all_provider_ids), 1), timeout_s=180.0)
        orch_restarted = True
        # Capability advertise after reconnect can re-insert model_provider rows —
        # re-apply catalogue restrict before the post-restart deletes.
        for model in models:
            restrict_model_to_providers(model, [keep])
        # Second pass: unload anything that survived reconnect / local restore.
        for model in models:
            if others:
                deleted.extend(delete_model_lanes_on_providers(admin, model, others))

    # Workers / planner may restore local engines shortly after reconnect, and
    # capability advertise may re-insert model_provider rows — keep re-restricting.
    settle_deadline = time.monotonic() + 60.0
    remaining: list[tuple[int, str, str]] = []
    while time.monotonic() < settle_deadline:
        for model in models:
            restrict_model_to_providers(model, [keep])
        remaining = []
        for provider_id in others:
            for lane in admin.node_lanes(int(provider_id)):
                model = str(lane.get("model") or "")
                if model in models:
                    remaining.append((int(provider_id), model, str(lane.get("lane_id") or "")))
        if not remaining:
            # Require a short clean window so a late restore does not race the test.
            time.sleep(3.0)
            for model in models:
                restrict_model_to_providers(model, [keep])
            late = []
            for provider_id in others:
                for lane in admin.node_lanes(int(provider_id)):
                    model = str(lane.get("model") or "")
                    if model in models:
                        late.append((int(provider_id), model, str(lane.get("lane_id") or "")))
            if not late:
                remaining = []
                break
            remaining = late
        for provider_id, model, lane_id in remaining:
            if lane_id:
                try:
                    admin.delete_lane(provider_id, lane_id)
                    deleted.append((provider_id, lane_id))
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(1.0)
    if remaining:
        raise RuntimeError(f"lifecycle isolation incomplete; foreign lanes remain: {remaining}")

    return {
        "keep_provider_id": keep,
        "foreign_provider_ids": others,
        "restricted_models": models,
        "deleted_lanes": deleted,
        "orchestrator_restarted": orch_restarted,
        "setup": "restrict_model_provider+lanes_delete_pre_post_orch_restart+settle",
    }


def seed_calibrated_profiles(provider_ids: list[int]) -> None:
    """Insert calibrated model_profiles rows so the planner may cold-load.

    Uses fixture ``loaded_vram_mb`` directly — does not add kv_budget again.
    """
    now_epoch = time.time()
    for provider_id in provider_ids:
        for model, profile in BENCHMARK_MODEL_PROFILES.items():
            base = float(profile["base_residency_mb"])
            loaded = float(profile.get("loaded_vram_mb", base))
            sleeping = float(profile["sleeping_residual_mb"])
            kv = float(profile.get("kv_budget_mb") or 0.0)
            tp = int(profile.get("tensor_parallel_size") or 1)
            max_ctx = int(profile.get("max_context_length") or 8192)
            pairs = profile.get("kv_cache_to_max_model_len_pairs") or [
                {"kv_cache_mb": int(kv or 1024), "max_model_len": max_ctx}
            ]
            payload = {
                "base_residency_mb": base,
                "loaded_vram_mb": loaded,
                "sleeping_residual_mb": sleeping,
                "kv_budget_mb": kv,
                "tensor_parallel_size": tp,
                "max_context_length": max_ctx,
                "last_measured_epoch": now_epoch,
                "measurement_count": 1,
                "residency_source": "calibrated",
                "engine": "vllm",
                "enable_sleep_mode": True,
                "kv_cache_to_max_model_len_pairs": pairs,
                # Functional simulated timings only (not measured GPU claims).
                "cold_load_time_s": float(profile.get("cold_load_time_s") or 1.5),
                "wake_from_sleep_time_s": float(profile.get("wake_from_sleep_time_s") or 0.3),
            }
            # Omit host_ram_* / disk footprints from observed fixtures: the
            # Docker Desktop host has ~8 GB RAM and would refuse every load
            # against real residual figures (tens of GB). VRAM ledger stays real.
            profile_json = json.dumps(payload).replace("'", "''")
            _psql(f"""
                INSERT INTO model_profiles (
                    provider_id, model_name,
                    base_residency_mb, loaded_vram_mb, sleeping_residual_mb, kv_budget_mb,
                    engine, tensor_parallel_size, max_context_length, max_reported_context_length,
                    residency_source, measurement_count, last_measured_at,
                    profile, sync_revision
                ) VALUES (
                    {provider_id}, '{model}',
                    {base}, {loaded},
                    {sleeping}, {kv},
                    'vllm', {tp}, {max_ctx}, {max_ctx},
                    'calibrated', 1, to_timestamp({now_epoch}),
                    '{profile_json}'::jsonb, 1
                )
                ON CONFLICT (provider_id, model_name) DO UPDATE SET
                    base_residency_mb = EXCLUDED.base_residency_mb,
                    loaded_vram_mb = EXCLUDED.loaded_vram_mb,
                    sleeping_residual_mb = EXCLUDED.sleeping_residual_mb,
                    kv_budget_mb = EXCLUDED.kv_budget_mb,
                    engine = EXCLUDED.engine,
                    tensor_parallel_size = EXCLUDED.tensor_parallel_size,
                    max_context_length = EXCLUDED.max_context_length,
                    max_reported_context_length = GREATEST(
                        COALESCE(model_profiles.max_reported_context_length, 0),
                        EXCLUDED.max_reported_context_length
                    ),
                    residency_source = 'calibrated',
                    measurement_count = EXCLUDED.measurement_count,
                    last_measured_at = EXCLUDED.last_measured_at,
                    profile = EXCLUDED.profile,
                    sync_revision = model_profiles.sync_revision + 1,
                    updated_at = CURRENT_TIMESTAMP;
                """)


def restart_ada_workers() -> None:
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    for service in BENCHMARK_NODE_SERVICES:
        compose._compose("restart", service, check=False, benchmark_topology=True)


def restart_orchestrator() -> None:
    """Refresh in-memory deployment/permission caches after SQL seeds.

    Fresh DB volumes + ``INSERT`` into ``models`` / permission tables leave the
    running orchestrator serving an empty deployment map until restart, which
    surfaces as ``No models passed classification`` on `/v1`.
    """
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    compose._compose("restart", "logos-orchestrator", check=False, benchmark_topology=True)
    compose.wait_for_orchestrator()


def bootstrap(*, fixture: Path | None = None) -> list:
    if fixture is not None or __import__("os").environ.get("E2E_PROFILE_FIXTURE"):
        load_profile_fixture(fixture)
    if not compose.is_up():
        raise RuntimeError("orchestrator not reachable — start the benchmark-topology stack first")
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES
    seed_models_and_permissions()
    nodes = compose.wait_for_nodes(expected=len(BENCHMARK_NODE_SERVICES))
    provider_ids = [int(n.provider_id) for n in nodes]
    seed_provider_links(provider_ids)
    seed_calibrated_profiles(provider_ids)
    # Orch first so `/v1` sees seeded model_provider rows; then workers re-fetch
    # calibrated profiles and advertise capabilities.
    restart_orchestrator()
    restart_ada_workers()
    time.sleep(5.0)
    nodes = compose.wait_for_nodes(expected=len(BENCHMARK_NODE_SERVICES), timeout_s=180.0)
    provider_ids = [int(n.provider_id) for n in nodes]
    seed_provider_links(provider_ids)
    seed_calibrated_profiles(provider_ids)
    # Touch sync_revision again and give the bridge a moment to adopt profiles
    # without a second restart (which would race capability advertise).
    time.sleep(3.0)
    return nodes


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed calibrated profiles for dual-Ada E2E")
    parser.add_argument(
        "--profile-fixture",
        type=Path,
        help="observed profiles JSON (nodes → models → calibrated fields)",
    )
    parser.add_argument(
        "--prepare-lifecycle",
        action="store_true",
        help=(
            "After bootstrap (or against a live stack), restrict target/pressure "
            "models to node-ada-a and delete foreign engines via lanes/delete. "
            "Does not start/stop compose."
        ),
    )
    parser.add_argument(
        "--lifecycle-target",
        default="google/gemma-3-4b-it",
        help="model exclusive to node-ada-a for sleep/wake proof",
    )
    parser.add_argument(
        "--lifecycle-pressure",
        default="Qwen/Qwen3.6-35B-A3B",
        help="TP-heavy model also pinned to node-ada-a for capacity pressure",
    )
    args = parser.parse_args()
    try:
        if args.prepare_lifecycle and compose.is_up():
            # Allow operators/CI to isolate without re-seeding profiles.
            compose._ACTIVE_BENCHMARK_TOPOLOGY = True
            compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES
            nodes = compose.wait_for_nodes(expected=len(BENCHMARK_NODE_SERVICES), timeout_s=120.0)
        else:
            nodes = bootstrap(fixture=args.profile_fixture)
    except Exception as exc:  # noqa: BLE001
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1
    with AdminClient(compose.orchestrator_url(), compose.ADMIN_KEY) as admin:
        for node in nodes:
            status = admin.node_status(node.provider_id)
            caps = (status.get("runtime") or {}).get("capabilities_models") or []
            print(f"provider {node.provider_id} ({node.name}): caps={caps}")
        if args.prepare_lifecycle:
            keep = next(
                (int(n.provider_id) for n in nodes if str(n.name) == BENCHMARK_NODE_SERVICES[0]),
                int(nodes[0].provider_id),
            )
            facts = prepare_lifecycle_isolation(
                admin,
                target_model=args.lifecycle_target,
                keep_provider_id=keep,
                also_restrict=[args.lifecycle_pressure],
                all_provider_ids=[int(n.provider_id) for n in nodes],
            )
            print(f"lifecycle isolation: {facts}")
    print(f"bootstrap ok: {len(nodes)} node(s); profiles={list(BENCHMARK_MODEL_PROFILES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
