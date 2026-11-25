"""Run against an explicitly supplied disposable PostgreSQL database."""

import copy
import os
import uuid
from datetime import date, datetime, time
from pathlib import Path
from xml.etree import ElementTree

import pytest
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.orm import Session

from logos.benchmarks import nightly_store as store
from logos.benchmarks.nightly import CAPACITY_WAITING
from logos.maintenance_window import MaintenanceWindow

pytestmark = pytest.mark.integration


@pytest.fixture
def database(monkeypatch):
    url = os.getenv("NIGHTLY_TEST_DB_URL")
    if not url:
        pytest.skip("Set NIGHTLY_TEST_DB_URL to an isolated PostgreSQL test database")
    schema = "nightly_test_" + uuid.uuid4().hex
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    migration = Path(
        os.getenv("NIGHTLY_SCHEMA_FILE")
        or (
            Path(__file__).resolve().parents[4]
            / "logos-webservice/src/main/resources/liquibase/changelog/065_nightly_benchmarks.xml"
        )
    )
    tree = ElementTree.parse(migration)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE providers (id integer PRIMARY KEY, name text)"))
        connection.execute(text("CREATE TABLE models (id integer PRIMARY KEY, name text)"))
        connection.execute(text("INSERT INTO providers VALUES (1, 'worker-a'), (2, 'worker-b')"))
        connection.execute(text("INSERT INTO models VALUES (11, 'model-a')"))
        connection.execute(
            text("CREATE TABLE model_provider (id integer PRIMARY KEY, provider_id integer, model_id integer)")
        )
        connection.execute(text("INSERT INTO model_provider VALUES (31, 1, 11), (32, NULL, NULL)"))
        connection.execute(
            text(
                "CREATE TABLE jobs(id integer PRIMARY KEY, environment text, status text, "
                "created_at timestamptz DEFAULT CURRENT_TIMESTAMP, "
                "updated_at timestamptz DEFAULT CURRENT_TIMESTAMP, result_payload jsonb, "
                "request_payload jsonb, error_message text)"
            )
        )

        for sql in tree.findall("{*}changeSet/{*}sql"):
            connection.execute(text(sql.text))
        connection.execute(text("CREATE TABLE notifications(id integer PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE users(id integer PRIMARY KEY, username text)"))
        connection.execute(
            text(
                "CREATE TABLE log_entry(id integer PRIMARY KEY, model_id integer, privacy_level text, "
                "result_status text, environment text, input_payload jsonb, user_id integer, "
                "timestamp_request timestamptz, timestamp_response timestamptz)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE model_provider_benchmarks(id integer PRIMARY KEY, configuration jsonb, "
                "metrics jsonb, sample_size integer, model_provider_id integer DEFAULT 31)"
            )
        )
        workloads_migration = migration.with_name("068_benchmark_request_workloads.xml")
        for sql in ElementTree.parse(workloads_migration).findall("{*}changeSet/{*}sql"):
            connection.execute(text(sql.text))
        for sql in ElementTree.parse(migration.with_name("084_benchmark_workload_size.xml")).findall(
            "{*}changeSet/{*}sql"
        ):
            connection.execute(text(sql.text))
        for sql in ElementTree.parse(migration.with_name("069_nightly_request_sources.xml")).findall(
            "{*}changeSet/{*}sql"
        ):
            connection.execute(text(sql.text))
        for name in ("100_nightly_rotation_window.xml", "101_nightly_window_revision.xml"):
            for sql in ElementTree.parse(migration.with_name(name)).findall("{*}changeSet/{*}sql"):
                connection.execute(text(sql.text))

    class Database(store.DBManager):
        def __enter__(self):
            self.session = Session(engine)
            return self

    monkeypatch.setattr(store, "DBManager", Database)
    monkeypatch.setattr(store, "_init_engine", lambda: engine)
    from logos.benchmarks import recent_requests

    monkeypatch.setattr(recent_requests, "DBManager", Database)
    yield engine, Database
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


def test_rotation_window_is_saved_per_pair_and_rejects_a_stale_editor(database):
    default = MaintenanceWindow.from_env()
    custom = MaintenanceWindow(time(11, 15), time(13), default.timezone)
    stale = MaintenanceWindow(time(12), time(14), default.timezone)
    store.set_enabled(31, enabled=True, expected=False)

    assert store.set_window(31, custom, default)
    assert not store.set_window(31, stale, default)
    row = store.status(31)
    assert row["enabled"]
    assert (row["window_start"], row["window_end"]) == (custom.start, custom.end)
    assert store.status(32).get("window_start") is None
    assert not store.claim(31, date(2026, 11, 25), window=default)
    assert store.claim(31, date(2026, 11, 25), window=custom)


def test_moving_the_window_can_run_the_same_pair_again_without_spending_another_daily_slot(database):
    default = MaintenanceWindow.from_env()
    afternoon = MaintenanceWindow(time(13, 10), time(13, 30), default.timezone)
    day = date(2026, 11, 25)
    store.set_enabled(31, enabled=True, expected=False)
    assert store.claim(31, day, window=default)
    store.finish(31, "failed", [], None)
    assert not store.claim(31, day, window=default)
    assert store.set_window(31, default, default)
    assert not store.claim(31, day, window=default)

    assert store.set_window(31, afternoon, default)
    assert store.claim(31, day, window=afternoon)
    store.finish(31, "completed", [], None)
    assert not store.claim(31, day, window=afternoon)
    assert store.set_window(31, default, afternoon)
    assert store.claim(31, day, window=default)
    assert store.claimed_today(day) == 1


def test_frozen_inputs_survive_log_deletion_and_selection_is_compare_and_set(database, monkeypatch):
    import importlib

    from logos.benchmarks import workloads

    dbmodule = importlib.import_module("logos.dbutils.dbmanager")

    engine, Database = database
    monkeypatch.setattr(dbmodule, "DBManager", Database)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO log_entry(id, model_id, privacy_level, result_status, environment, input_payload) "
                "VALUES (7, 11, 'FULL', 'success', '', "
                '\'{"messages":[{"role":"user","content":"Synthetic prompt"}]}\')'
            )
        )
    created = workloads.create([7], "Trial", "synthetic")
    assert created["created_at"]
    assert workloads.create([7], "Same inputs, another name", "synthetic") == created
    store.set_enabled(31, False, False, created["id"], None)
    with pytest.raises(ValueError):
        store.set_enabled(31, False, False, None, None)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM log_entry WHERE id = 7"))
    assert workloads.load(created["id"], 11)["requests"][0]["body"]["messages"][0]["content"] == "Synthetic prompt"
    with pytest.raises(ValueError):
        workloads.create([7], "Missing", "synthetic")
    assert store.status(31)["workload_id"] == created["id"]


def test_recent_workload_window_filters_and_freezes_hundreds_of_inputs(database, monkeypatch):
    from importlib import import_module

    from logos.benchmarks import workloads
    from logos.benchmarks.configuration import BenchmarkRunSettings
    from logos.dbutils.dbrequest import InternalBenchmarkRequest

    engine, database_type = database
    monkeypatch.setattr(import_module("logos.dbutils.dbmanager"), "DBManager", database_type)
    target = {"model_id": 11, "model_name": "model-a"}
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO log_entry(id, model_id, privacy_level, result_status, input_payload,
                                  timestamp_request, timestamp_response)
            SELECT n, 11, 'FULL', 'success',
                   '{"messages":[{"role":"user","content":"demo"}],"user":"private","headers":{"x":"private"}}'::jsonb,
                   now() - interval '2 days', now() - interval '2 days'
            FROM generate_series(1,510) n
        """))
        connection.execute(text("UPDATE log_entry SET privacy_level='BASIC' WHERE id=510"))
        connection.execute(text("UPDATE log_entry SET result_status='failure' WHERE id=509"))
        connection.execute(text("UPDATE log_entry SET environment='model-provider-benchmark' WHERE id=508"))
        connection.execute(text("UPDATE log_entry SET model_id=12 WHERE id=507"))
        connection.execute(text("UPDATE log_entry SET input_payload='{}'::jsonb WHERE id=506"))
        connection.execute(text("UPDATE log_entry SET timestamp_response=now() + interval '1 day' WHERE id=505"))
    with pytest.raises(ValueError, match="No replayable"):
        workloads.create_recent(target, 1)
    created = workloads.create_recent(target, 3)
    assert created["request_count"] == 500 and "requests" not in created
    assert workloads.create_recent(target, 3) == created
    # A low-traffic model can need logs older than the longest time window.
    with engine.begin() as connection:
        connection.execute(text("UPDATE log_entry SET timestamp_request = timestamp_request - interval '8 days'"))
        connection.execute(
            text(
                "UPDATE log_entry SET timestamp_request = now() - interval '1 hour', "
                "timestamp_response = now() WHERE id=1"
            )
        )
    latest = workloads.create_recent(target)
    assert latest["request_count"] == 100 and latest["provenance"] == "production"
    assert workloads.create_recent(target) == latest
    selected_ids = [row["source_log_id"] for row in workloads.load(latest["id"], 11)["requests"]]
    assert selected_ids == [1, *range(504, 405, -1)]
    partial = workloads.create_recent(target, 7)
    assert partial["request_count"] == 1
    for settings in [BenchmarkRunSettings, InternalBenchmarkRequest]:
        args = {"model_provider_id": 31} if settings == InternalBenchmarkRequest else {}
        assert settings(samples=500, workload_id=created["id"], **args).samples == 500
        with pytest.raises(ValueError):
            settings(samples=101, **args)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM log_entry"))
    saved = workloads.load(created["id"], 11)["requests"]
    assert [row["source_log_id"] for row in saved] == list(range(504, 4, -1))
    assert saved[0]["body"] == {"messages": [{"role": "user", "content": "demo"}]}


def test_recent_workload_endpoint_uses_logged_inputs_when_only_the_pair_is_given(database, monkeypatch):
    from importlib import import_module

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    router = import_module("logos.routers.nightly_benchmarks")
    engine, database_type = database
    monkeypatch.setattr(import_module("logos.dbutils.dbmanager"), "DBManager", database_type)
    monkeypatch.setattr(import_module("logos.routers.internal"), "_INTERNAL_SECRET", "test-only")
    monkeypatch.setattr(router, "target_for", {31: {"model_id": 11, "model_name": "model-a"}}.__getitem__)
    app = FastAPI()
    app.include_router(router.router)
    path = "/internal/model_benchmarks/nightly/workloads"
    with TestClient(app) as client:
        assert client.post(path, json={"model_provider_id": 31}).status_code == 401
        client.headers["Authorization"] = "Bearer test-only"
        assert client.post(path, json={"model_provider_id": 31}).status_code == 400
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO log_entry(id, model_id, privacy_level, result_status, input_payload,
                                      timestamp_request, timestamp_response)
                VALUES (1, 11, 'FULL', 'success', '{"messages":[{"role":"user","content":"test"}]}',
                        now() - interval '1 month', now() - interval '1 month')
            """))
        result = client.post(path, json={"model_provider_id": 31})
        assert result.status_code == 200
        assert result.json()["provenance"] == "production" and result.json()["request_count"] == 1
        assert "requests" not in result.json()
        assert client.post(path, json={"model_provider_id": 31, "lookback_days": 3}).status_code == 400


async def test_completion_event_survives_failed_delivery_and_deduplicates_finish(database, monkeypatch):
    from importlib import import_module
    from unittest.mock import AsyncMock

    notifications = import_module("logos.notifications")

    engine, _ = database
    store.set_enabled(31, True, False)
    store.claim(31, date(2026, 10, 28))
    store.finish(31, "completed", [])
    store.finish(31, "completed", [])
    delivery = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(notifications, "emit_notification", delivery)
    await store.deliver_completion_events()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM benchmark_completion_events WHERE delivered_at IS NULL")
            ).scalar_one()
            == 1
        )
    await store.deliver_completion_events()
    await store.deliver_completion_events()
    assert delivery.await_count == 2
    assert delivery.call_args.kwargs["event_key"] == "nightly:31:2026-10-28"


def test_moving_a_window_keeps_one_completion_per_claimed_revision(database):
    engine, _ = database
    day = date(2026, 11, 25)
    default = MaintenanceWindow.from_env()
    afternoon = MaintenanceWindow(time(13, 10), time(13, 30), default.timezone)
    store.set_enabled(31, True, False)
    assert store.claim(31, day, window=default)
    store.finish(31, "continuing", [{"configuration": 1, "status": "success"}])

    assert store.set_window(31, afternoon, default)
    store.finalize_continuations(datetime.fromisoformat("2026-11-25T07:00:00+01:00"))
    assert store.claim(31, day, window=afternoon)
    report = [{"configuration": 1, "status": "failed", "reason": "The afternoon baseline failed"}]
    store.finish(31, "failed", report)
    store.finish(31, "failed", report)

    with engine.connect() as connection:
        events = dict(connection.execute(text("SELECT event_id, payload FROM benchmark_completion_events")).all())
    morning = events["nightly:31:2026-11-25"]
    later = events["nightly:31:2026-11-25:1"]
    assert len(events) == 2
    assert (morning["status"], morning["rounds"], morning["successful_configurations"]) == ("completed", 1, 1)
    assert (later["status"], later["rounds"], later["successful_configurations"]) == ("failed", 1, 0)
    assert later["interruption_reason"] == "The afternoon baseline failed"


@pytest.mark.parametrize("adopt", [False, True])
def test_finish_persists_winner_reference_and_chart_eligibility_atomically(database, adopt):
    import json

    engine, _ = database
    store.set_enabled(31, True, False)
    store.claim(31, date(2026, 10, 28))
    search = {
        "state": {"series_key": "series", "workload_id": "a" * 64},
        "reference_id": 1,
        "series_key": "series",
        "workload_id": "a" * 64,
    }
    with engine.begin() as connection:
        for identifier, throughput in [(1, 40), (2, 50)]:
            configuration = {
                "workload": {"id": "a" * 64},
                "nightly": {
                    "baseline_id": 1,
                    "reference_id": 1,
                    "series_key": "series",
                    "parameters": {"max_num_seqs": 16 * identifier},
                },
            }
            metrics = {
                "aggregate_output_tokens_per_second": throughput,
                "request_totals": {"total": 1, "successful": 1, "errored": 0, "incomplete": 0},
                "replay_samples": [{"source_log_id": 7, "finish_reason": "stop", "output_tokens": 100}],
                "request_latency_ms": {"successful": {"percentiles": {"p95": 1000}}},
            }
            connection.execute(
                text(
                    "INSERT INTO model_provider_benchmarks "
                    "VALUES (:id, CAST(:config AS jsonb), CAST(:metrics AS jsonb), 1)"
                ),
                {"id": identifier, "config": json.dumps(configuration), "metrics": json.dumps(metrics)},
            )
    report = [{"configuration": i, "status": "success", "benchmark_id": i} for i in (1, 2)]
    candidate = store.adoption_candidate(31, search, report)
    assert candidate["benchmark_id"] == 2
    assert store.adoption_candidate(32, search, report) is None
    if adopt:
        report.append({"status": "adopted", **candidate})
    store.finish(31, "completed", report, search)
    assert store.status(31)["optimizer"]["best_parameters"] == {"max_num_seqs": 32}
    assert store.status(31)["optimizer"]["reference_id"] == 1
    if adopt:
        assert store.status(31)["optimizer"]["production_adoption"]["benchmark_id"] == 2
    with engine.connect() as connection:
        payload = connection.execute(text("SELECT payload FROM benchmark_completion_events")).scalar_one()
        assert payload["benchmark_id"] == 2
        assert payload["changes"] == [{"parameter": "max_num_seqs", "before": None, "after": 32}]
        assert (
            connection.execute(
                text(
                    "SELECT configuration->'nightly'->>'eligible_for_selection' "
                    "FROM model_provider_benchmarks WHERE id=2"
                )
            ).scalar_one()
            == "true"
        )
        assert (
            connection.execute(
                text("SELECT payload->>'best_benchmark_id' FROM benchmark_completion_events")
            ).scalar_one()
            == "2"
        )


def test_opt_in_compare_and_set_and_persistent_daily_claim(database):
    assert store.status(31)["enabled"] is False
    assert store.set_enabled(31, True, False)["enabled"] is True
    with pytest.raises(ValueError):
        store.set_enabled(31, True, False)
    assert store.claim(31, date(2026, 10, 13))
    assert not store.claim(31, date(2026, 10, 13))
    assert not store.claim(31, date(2026, 10, 12))
    assert store.claim(31, date(2026, 10, 14))
    store.set_enabled(31, False, True)
    assert not store.claim(31, date(2026, 10, 15))


def test_cluster_lock_excludes_other_scheduler_and_manual_runs(database):
    _, Database = database
    with store.cluster_lock() as first:
        assert first is not None
        assert store.lock_held(first)
        with store.cluster_lock() as second:
            assert second is None
        with Database() as db:
            assert not store.manual_admission(db, 20)
    with Database() as db:
        assert store.manual_admission(db, 20)


def test_recovery_journal_blocks_manual_admission_after_lock_release(database):
    _, Database = database
    store.set_enabled(31, True, False)
    store.save(31, "recovery_required", [{"status": "stalled"}], {"provider_id": 20})
    with Database() as db:
        assert not store.manual_admission(db, 20)
    assert store.status(31)["recovery_required"]
    assert "recovery" not in store.status(31)
    store.set_enabled(31, False, True)
    with pytest.raises(ValueError):
        store.set_enabled(31, True, False)
    store.save(31, "recovered", [], None)
    with Database() as db:
        assert store.manual_admission(db, 20)


def test_progress_does_not_replace_job_result_or_renew_a_failed_job(database):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs(id, status, result_payload) VALUES "
                "(1, 'running', '{\"stage\":\"benchmarking\"}'), (2, 'failed', '{}')"
            )
        )
    store.write_progress(1, 25, 50)
    store.write_progress(2, 25, 50)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT result_payload FROM jobs WHERE id=1")).scalar() == {
            "stage": "benchmarking",
            "completed_samples": 25,
            "total_samples": 50,
        }
        assert connection.execute(text("SELECT result_payload FROM jobs WHERE id=2")).scalar() == {}


def test_deleted_deployment_keeps_recovery_and_disables_future_runs(database):
    engine, _ = database
    store.set_enabled(31, True, False)
    store.save(31, "recovery_required", [], {"provider_id": 20})
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM model_provider WHERE id = 31"))
    store.disable_missing_target(31)
    assert store.status(31)["enabled"] is False
    assert store.rows()[0]["recovery"] == {"provider_id": 20}


def test_two_pair_rotation_persists_across_restart_failure_and_disable(database):
    engine, _ = database
    for pair in (31, 32, 33, 34):
        store.set_enabled(pair, True, False)
    first = date(2026, 10, 13)
    with store.cluster_lock():
        assert store.claim(31, first)
        assert store.claim(32, first)
        assert not store.claim(33, first)
    store.save(31, "failed", [], None)
    store.set_enabled(31, False, True)
    engine.dispose()  # New connections must see the consumed nightly quota.
    assert store.claimed_today(first) == 2
    with store.cluster_lock():
        assert not store.claim(33, first)
    second = date(2026, 10, 14)
    waiting = [row["model_provider_id"] for row in store.rows() if row["enabled"]]
    assert waiting == [33, 34, 32]
    with store.cluster_lock():
        assert store.claim(waiting[0], second)
        assert store.claim(waiting[1], second)
        assert not store.claim(waiting[2], second)
    store.set_enabled(31, True, False)
    assert [row["model_provider_id"] for row in store.rows()] == [31, 32, 33, 34]


def test_calibration_claim_blocks_manual_admission_and_survives_connections(database):
    engine, Database = database
    with store.cluster_lock():
        store.claim_calibration(20, "owner", automatic=True)
        store.confirm_calibration("owner")
    engine.dispose()
    assert store.calibration_claims()[0]["confirmed"]
    assert store.calibration_claims()[0]["automatic"]
    with Database() as db:
        assert not store.manual_admission(db, 20)
    store.release_calibration("old-owner")
    assert store.calibration_claims()
    store.release_calibration("owner")
    with Database() as db:
        assert store.manual_admission(db, 20)


def test_finished_job_keeps_slot_until_benchmark_cleanup_completes(database):
    engine, Database = database
    with Database() as db:
        assert store.manual_admission(db, 20)
        store.claim_benchmark(db, 7, 20)
    # A second connection must not admit another job, even without a live jobs row.
    with Database() as db:
        assert not store.manual_admission(db, 20)
    assert store.has_active_benchmarks()
    store.release_benchmark(6)
    assert store.has_active_benchmarks()
    store.release_benchmark(7)
    assert not store.has_active_benchmarks()
    with Database() as db:
        assert store.manual_admission(db, 20)


def test_old_running_job_is_not_assumed_finished(database):
    engine, Database = database
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs(id, environment, status, updated_at) VALUES "
                "(7, 'model-provider-benchmark', 'running', CURRENT_TIMESTAMP - INTERVAL '1 day')"
            )
        )
    assert store.has_active_benchmarks()
    with Database() as db:
        assert not store.manual_admission(db, 20)


def test_execution_lease_survives_maintenance_lock_release(database):
    with store.execution_lock(31) as execution:
        assert execution is not None
        assert store.execution_lock_held(execution, 31)
        with store.cluster_lock() as first:
            assert first is not None
        # Another scheduler can hold maintenance, but cannot recover a live run.
        with store.cluster_lock() as recovery:
            assert recovery is not None
            with store.execution_lock(31) as blocked:
                assert blocked is None
    with store.execution_lock(31) as recovered:
        assert recovered is not None


def test_orphan_fencing_targets_only_its_nightly_jobs_and_preserves_manual_claim(database):
    engine, Database = database
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO jobs(id, environment, status, request_payload) VALUES
            (7, 'model-provider-benchmark', 'running', '{"nightly": true, "model_provider_id": 31}'),
            (8, 'model-provider-benchmark', 'running', '{"nightly": false, "model_provider_id": 31}'),
            (9, 'model-provider-benchmark', 'running', '{"nightly": true, "model_provider_id": 32}')
        """))
    assert store.recovery_jobs(31) == [7]
    assert store.has_active_benchmarks(excluding=[7])
    assert store.benchmark_requests_allowed(7)
    store.fence_recovery_jobs([7])
    assert not store.benchmark_requests_allowed(7)
    assert store.benchmark_requests_allowed(8)
    with Database() as db:
        assert not db.touch_model_benchmark_job(7)
        assert db.touch_model_benchmark_job(8)
    store.finish_recovery_jobs([7])
    assert store.recovery_jobs(31) == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM jobs WHERE id=7")).scalar() == "failed"
    with Database() as db:
        store.claim_benchmark(db, 8, 20)
    assert store.has_active_benchmarks(excluding=[8, 9])
    store.release_benchmark(8)
    assert not store.has_active_benchmarks(excluding=[8, 9])


def test_parallel_manual_claims_are_durable_bounded_and_provider_exclusive(database, monkeypatch):
    engine, Database = database
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "2")
    with Database() as db:
        assert store.manual_admission(db, 20)
        store.claim_benchmark(db, 7, 20)
    with Database() as db:
        assert not store.manual_admission(db, 20)
    with Database() as db:
        assert store.manual_admission(db, 21)
        store.claim_benchmark(db, 8, 21)
    engine.dispose()
    with Database() as db:
        assert not store.manual_admission(db, 22)
    store.release_benchmark(7)
    with Database() as db:
        assert store.manual_admission(db, 22)
        store.claim_benchmark(db, 9, 22)
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "1")
    with Database() as db:
        assert not store.manual_admission(db, 20)


def test_parallel_calibration_rows_survive_restart_and_release_only_matching_token(database):
    engine, Database = database
    with store.cluster_lock():
        store.claim_calibration(20, "first", automatic=True)
        store.claim_calibration(21, "second", automatic=True)
        store.confirm_calibration("first")
        store.confirm_calibration("second")
    engine.dispose()
    assert {claim["provider_id"] for claim in store.calibration_claims()} == {20, 21}
    store.release_calibration("stale-token")
    assert len(store.calibration_claims()) == 2
    store.release_calibration("first")
    assert [claim["provider_id"] for claim in store.calibration_claims()] == [21]
    with Database() as db:
        assert not store.manual_admission(db, 22)


def test_manual_job_and_cleanup_claim_count_once(database, monkeypatch):
    engine, Database = database
    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "2")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs(id, environment, status, request_payload) VALUES "
                "(7, 'model-provider-benchmark', 'running', '{\"provider_id\": 20}')"
            )
        )
    with Database() as db:
        store.claim_benchmark(db, 7, 20)
    with Database() as db:
        assert store.manual_admission(db, 21)
    with Database() as db:
        assert not store.manual_admission(db, 20)


def test_comparison_metadata_survives_database_session_close(database):
    engine, _ = database
    with engine.begin() as connection:
        connection.execute(
            text('INSERT INTO model_provider_benchmarks(id, configuration) VALUES (1, \'{"tool":"guidellm"}\')')
        )
    store.record_comparison(1, 1, {"max_num_seqs": 16}, {"max_num_seqs": 16})
    with engine.connect() as connection:
        config = connection.execute(
            text("SELECT configuration FROM model_provider_benchmarks WHERE id = 1")
        ).scalar_one()
        assert config["nightly"]["baseline_id"] == 1
        assert config["tool"] == "guidellm"


def test_maintenance_holders_name_every_kind_of_slot_holder(database):
    engine, _ = database
    store.claim_calibration(2, "claim-2", automatic=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (id, environment, status, request_payload) VALUES "
                "(5, 'model-provider-benchmark', 'running', "
                '\'{"provider_id": 1, "model_name": "model-a", "nightly": true}\'), '
                "(6, 'model-provider-benchmark', 'failed', '{\"provider_id\": 2, \"model_name\": \"model-b\"}'), "
                "(7, 'inference', 'running', '{}'), "
                "(8, 'model-provider-benchmark', 'completed', '{\"provider_id\": 2}')"
            )
        )
        # A finished job whose claim is still being cleaned up holds the slot too.
        connection.execute(text("INSERT INTO maintenance_benchmark (job_id, provider_id) VALUES (6, 2)"))
        connection.execute(
            text(
                "INSERT INTO nightly_benchmarks (model_provider_id, status, recovery) "
                'VALUES (31, \'restoring\', \'{"provider_id": 1, "model": "model-a"}\')'
            )
        )
    holders = store.maintenance_holders()
    (claim,) = holders["calibrations"]
    assert (claim["provider_id"], claim["provider_name"], claim["token"], claim["automatic"]) == (
        2,
        "worker-b",
        "claim-2",
        True,
    )
    assert claim["age_s"] >= 0
    benchmarks = {row["job_id"]: row for row in holders["benchmarks"]}
    assert set(benchmarks) == {5, 6}
    assert benchmarks[5]["provider_name"] == "worker-a"
    assert benchmarks[5]["model_name"] == "model-a"
    assert benchmarks[5]["nightly"] is True
    # A cancelled job keeps its status and model name while its claim is cleaned up.
    assert benchmarks[6]["status"] == "failed"
    assert (benchmarks[6]["provider_name"], benchmarks[6]["model_name"]) == ("worker-b", "model-b")
    (recovery,) = holders["recoveries"]
    assert (recovery["model_provider_id"], recovery["provider_name"], recovery["model_name"]) == (
        31,
        "worker-a",
        "model-a",
    )
    assert recovery["status"] == "restoring"


@pytest.mark.parametrize("disabled", [False, True])
def test_continuous_rounds_keep_daily_claims_and_notify_only_after_the_final_restoration(database, disabled):
    engine, _ = database
    day = date(2026, 10, 29)
    store.set_enabled(31, True, False)
    assert store.claim(31, day)
    report = [{"configuration": 1, "status": "success"}, {"configuration": 2, "status": "success"}]
    store.finish(31, "continuing", report)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM benchmark_completion_events")).scalar_one() == 0
    assert store.claim(31, day)
    assert not store.claim(31, day)

    assert store.claimed_today(day) == 1
    store.finish(31, "continuing", report)
    store.finalize_continuations(datetime.fromisoformat("2026-10-29T07:59:59+01:00"))
    assert store.status(31)["status"] == "continuing"
    assert store.status(31)["optimizer"]["nightly_session"]["rounds"] == 2
    with pytest.raises(ValueError):
        store.set_enabled(31, True, True, None, None)
    if disabled:
        store.set_enabled(31, False, True)
    stamp = "2026-10-29T07:59:59+01:00" if disabled else "2026-10-29T08:00:00+01:00"
    store.finalize_continuations(datetime.fromisoformat(stamp))
    store.finalize_continuations(datetime.fromisoformat(stamp))
    assert store.status(31)["status"] == ("disabled" if disabled else "completed")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM benchmark_completion_events")).scalar_one() == 1
        payload = connection.execute(text("SELECT payload FROM benchmark_completion_events")).scalar_one()
        assert payload["successful_configurations"] == 4
        assert payload["rounds"] == 2
    assert not store.claim(31, day)


def test_capacity_wait_reuses_its_daily_claim_and_emits_one_deferred_event(database):
    engine, _ = database
    day = date(2026, 10, 29)
    report = [{"status": CAPACITY_WAITING, "reason": "GPU 0 lacks free memory for warm-up"}]
    store.set_enabled(31, True, False)
    assert store.claim(31, day)
    store.finish(31, CAPACITY_WAITING, report)

    store.finalize_continuations(datetime.fromisoformat("2026-10-29T07:59:59+01:00"))
    assert store.status(31)["status"] == CAPACITY_WAITING
    workload_id = "c" * 64
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO benchmark_workloads(id, name, model_id, model_name, provenance, requests, request_count) "
                "VALUES (:id, 'Next workload', 11, 'model-a', 'synthetic', '[{}]'::jsonb, 1)"
            ),
            {"id": workload_id},
        )
    updated = store.set_enabled(31, True, True, workload_id, None)
    assert updated["workload_id"] == workload_id
    assert updated["status"] == CAPACITY_WAITING
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM benchmark_completion_events")).scalar_one() == 0
    assert store.claim(31, day)
    assert store.claimed_today(day) == 1
    store.finish(31, CAPACITY_WAITING, report)

    end = datetime.fromisoformat("2026-10-29T08:00:00+01:00")
    store.finalize_continuations(end)
    store.finalize_continuations(end)
    assert store.status(31)["status"] == "capacity_deferred"
    with engine.connect() as connection:
        event = connection.execute(text("SELECT payload FROM benchmark_completion_events")).scalar_one()
    assert (event["status"], event["rounds"], event["successful_configurations"]) == ("capacity_deferred", 0, 0)
    assert not store.claim(31, day)


def test_recovery_resumes_the_same_claim_and_records_one_final_event(database):
    engine, _ = database
    day = date(2026, 10, 29)
    store.set_enabled(31, True, False)
    report = [{"status": "success"}, {"status": "recovery_required", "reason": "GPU capacity unavailable"}]

    for attempt in range(store.MAX_RECOVERY_RESUMES + 1):
        assert store.claim(31, day)
        assert store.claimed_today(day) == 1
        store.save(31, "recovery_required", report, {"provider_id": 1})
        store.finish(31, "interrupted_recovered", report, resume_after_recovery=True)
        state = store.status(31)
        assert not state["recovery_required"]
        if attempt < store.MAX_RECOVERY_RESUMES:
            assert state["status"] == "continuing"
            with engine.connect() as connection:
                assert connection.execute(text("SELECT COUNT(*) FROM benchmark_completion_events")).scalar_one() == 0

    assert state["status"] == "interrupted_recovered"
    assert not store.claim(31, day)
    with engine.connect() as connection:
        payload = connection.execute(text("SELECT payload FROM benchmark_completion_events")).scalar_one()
    assert payload["recovery_retry_limit_reached"]
    assert payload["interruption_reason"] == "GPU capacity unavailable"
    assert payload["rounds"] == 3


def test_selection_diagnostics_keep_workloads_separate_and_explain_rejected_faster_runs(database):
    from sqlalchemy import text
    from sqlalchemy.types import JSON

    from logos.benchmarks.optimization import eligible

    engine, _ = database
    baseline = {
        "id": 1,
        "sample_size": 1,
        "configuration": {"workload": {"id": "a"}, "nightly": {"baseline_id": 1, "series_key": "series"}},
        "metrics": {
            "aggregate_output_tokens_per_second": 40,
            "request_totals": {"total": 1, "successful": 1, "errored": 0, "incomplete": 0},
            "replay_samples": [{"source_log_id": 8, "output_tokens": 100, "finish_reason": "stop"}],
            "request_latency_ms": {"successful": {"percentiles": {"p95": 1000}}},
        },
    }

    faster = copy.deepcopy(baseline)
    faster["id"] = 2
    faster["metrics"]["aggregate_output_tokens_per_second"] = 50
    faster["metrics"]["replay_samples"][0]["output_tokens"] = 20
    other = copy.deepcopy(baseline)
    other["id"] = 3
    other["configuration"]["workload"]["id"] = "b"
    with engine.begin() as connection:
        statement = text(
            "INSERT INTO model_provider_benchmarks(id, configuration, metrics, sample_size) "
            "VALUES (:id, :configuration, :metrics, :sample_size)"
        ).bindparams(
            bindparam("configuration", type_=JSON),
            bindparam("metrics", type_=JSON),
        )
        connection.execute(statement, [baseline, faster, other])
    summary = store.selection_summary(31, "a")
    assert eligible(baseline, baseline)
    assert summary["best_observed"]["benchmark_id"] == 2
    assert summary["best_eligible"]["benchmark_id"] == 1
    assert summary["best_observed"]["issues"] == ["1 answer lengths differ by more than 20% from baseline"]
    assert {item["benchmark_id"] for item in summary["comparisons"]} == {1, 2}
    assert "source_log_id" not in str(summary)


async def test_failed_adoption_alert_is_delivered_even_with_pending_worker_recovery(database, monkeypatch):
    from importlib import import_module
    from unittest.mock import AsyncMock

    notifications = import_module("logos.notifications")

    failure = {
        "status": "adoption_failed",
        "benchmark_id": 164,
        "baseline_id": 160,
        "parameters": {"max_num_batched_tokens": 2304},
        "throughput": 32.8,
        "baseline_throughput": 22.9,
        "reason": "Could not make enough GPU capacity available to load the model.",
    }
    store.set_enabled(31, True, expected=False)
    journal = {"provider_id": 1}
    for _ in range(2):
        store.save(31, "recovery_required", [failure], journal, adoption_failure=failure)

    emit = AsyncMock(return_value=True)
    monkeypatch.setattr(notifications, "emit_notification", emit)
    await store.deliver_completion_events()
    await store.deliver_completion_events()

    assert store.status(31)["recovery_required"]
    emit.assert_awaited_once()
    notification_type, title, body, payload, recipients = emit.await_args.args
    assert notification_type == "benchmark_result_ready"
    assert title == "Nightly configuration adoption failed"
    assert "Benchmark #164" in body
    assert "Max batched tokens 2304" in body
    assert "32.8 tok/s (+43.2% vs. baseline #160)" in body
    assert failure["reason"] in body
    assert payload["model_id"] == 11 and payload["benchmark_id"] == 164
    assert recipients == [{"kind": "role", "role": "logos_admin"}]
    assert emit.await_args.kwargs["event_key"] == "nightly-adoption-failed:31:164"


async def test_manual_starts_share_admission_but_exclude_same_worker_and_automatic_dispatch(database, monkeypatch):
    import asyncio
    from unittest.mock import MagicMock

    from logos.benchmarks.maintenance import AUTOMATIC_CALIBRATION, MaintenanceCoordinator
    from logos.logosnode_registry import MaintenanceBusyError

    monkeypatch.setenv("LOGOS_MAINTENANCE_MAX_NODES", "1")
    service = MaintenanceCoordinator(MagicMock())
    sent, finish = asyncio.Event(), asyncio.Event()

    async def wait_for_ack(callback):
        callback(None)
        sent.set()
        await finish.wait()

    async def dispatch(callback):
        callback(None)
        return "started"

    first = asyncio.create_task(service.start_calibration(1, wait_for_ack))
    await asyncio.wait_for(sent.wait(), 5)
    try:
        assert await service.start_calibration(2, dispatch) == "started"
        with pytest.raises(MaintenanceBusyError):
            await service.start_calibration(1, dispatch)
        assert not store.claim_calibration(1, "other-replica")
        with store.cluster_lock() as exclusive:
            assert exclusive is None
    finally:
        finish.set()
        await first
    assert {row["provider_id"] for row in store.calibration_claims() if row["confirmed"]} == {1, 2}
    context = AUTOMATIC_CALIBRATION.set(True)
    try:
        with pytest.raises(MaintenanceBusyError):
            await service.start_calibration(3, dispatch)
    finally:
        AUTOMATIC_CALIBRATION.reset(context)
