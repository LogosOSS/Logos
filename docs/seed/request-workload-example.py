"""Illustrative request workload for the isolated documentation stack; no GPU measurements."""

import json
import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from logos.benchmarks.completion import notification_message, result
from logos.benchmarks.optimization import context
from logos.benchmarks.workloads import build_snapshot

if os.environ.get("LOGOS_DOCS_ENABLED") != "true":
    raise SystemExit("Run only in the isolated documentation orchestrator")

with psycopg2.connect(os.environ["LOGOS_DB_URL"]) as db:
    with db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT mp.id AS pair_id, mp.provider_id, p.name AS provider_name, m.id, m.name FROM model_provider mp "
            "JOIN providers p ON p.id = mp.provider_id JOIN models m ON m.id = mp.model_id "
            "WHERE p.name = 'Docs Local Worker' AND m.name = 'llama-3.1-8b-instruct'"
        )
        model = cursor.fetchone()
        if not model:
            raise SystemExit("Apply role-screenshots.sql first")
        rows = [
            {
                "id": index,
                "model_id": model["id"],
                "model_name": model["name"],
                "privacy_level": "FULL",
                "result_status": "success",
                "input_payload": {
                    "messages": [
                        {"role": "user", "content": f"Synthetic documentation task {index}: explain a transaction."}
                    ],
                    "temperature": 0.2,
                },
            }
            for index in range(1, 11)
        ]
        workload = build_snapshot(rows, "Coding pilot", "synthetic")
        cursor.execute(
            "SELECT min(recorded_at) AS frozen_at FROM model_provider_benchmarks "
            "WHERE model_provider_id = %s AND configuration->'metadata'->>'purpose' = 'docs-role-screenshots'",
            (model["pair_id"],),
        )
        frozen_at = cursor.fetchone()["frozen_at"]
        cursor.execute(
            "INSERT INTO benchmark_workloads "
            "(id, name, model_id, model_name, provenance, requests, request_count, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 10, COALESCE(%s, now())) "
            "ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name, "
            "created_at=LEAST(benchmark_workloads.created_at, EXCLUDED.created_at) "
            "RETURNING created_at",
            (
                workload["id"],
                workload["name"],
                model["id"],
                model["name"],
                "synthetic",
                Json(workload["requests"]),
                frozen_at,
            ),
        )
        workload["created_at"] = cursor.fetchone()["created_at"].isoformat()
        cursor.execute(
            "SELECT * FROM model_provider_benchmarks WHERE model_provider_id = %s "
            "AND configuration->'metadata'->>'purpose' = 'docs-role-screenshots' ORDER BY recorded_at, id",
            (model["pair_id"],),
        )
        runs = cursor.fetchall()
        if not runs:
            raise SystemExit("Apply the illustrative benchmark seed first")
        reference = runs[0]["id"]
        production = {"max_num_seqs": 16, "max_num_batched_tokens": 4096}
        runtime = {
            "runtime": {
                "devices": {"devices": [{"kind": "nvidia", "name": "Documentation GPU", "memory_total_mb": 46080}]}
            }
        }
        series = context(production, runtime, workload, {})["series_key"]
        for run in runs:
            configuration = run["configuration"]
            configuration.update(
                tool="guidellm",
                workload={key: workload[key] for key in ("id", "name", "provenance", "request_count", "created_at")},
            )
            nightly = configuration["nightly"]
            nightly.update(
                reference_id=reference, series_key=series, workload_id=workload["id"], eligible_for_selection=True
            )
            configuration["serving"] = {**production, **nightly.get("parameters", {})}
            run["sample_size"] = 10
            metrics = run["metrics"]
            rate = (
                metrics.get("aggregate_output_tokens_per_second")
                or metrics["output_tokens_per_second"]["successful"]["mean"]
            )
            metrics["aggregate_output_tokens_per_second"] = rate
            metrics["request_totals"] = {"total": 10, "successful": 10, "errored": 0, "incomplete": 0}
            metrics["replay_samples"] = [
                {"source_log_id": index, "output_tokens": 100, "finish_reason": "stop"} for index in range(1, 11)
            ]
            metrics["request_latency_ms"] = {"successful": {"percentiles": {"p95": 1000}}}
            cursor.execute(
                "UPDATE model_provider_benchmarks SET configuration=%s, metrics=%s, sample_size=10, dataset=%s "
                "WHERE id=%s",
                (Json(configuration), Json(metrics), "requests/" + workload["id"], run["id"]),
            )
        best = max(runs, key=lambda run: run["metrics"]["aggregate_output_tokens_per_second"])
        optimizer = {
            "series_key": series,
            "workload_id": workload["id"],
            "reference_id": reference,
            "best_benchmark_id": best["id"],
            "best_throughput": best["metrics"]["aggregate_output_tokens_per_second"],
            "best_parameters": {"max_num_seqs": 20, "max_num_batched_tokens": 4096},
            "iterations": 3,
            "production_adoption": {
                "benchmark_id": best["id"],
                "baseline_id": best["configuration"]["nightly"]["baseline_id"],
                "throughput": best["metrics"]["aggregate_output_tokens_per_second"],
                "baseline_throughput": 42,
            },
        }
        cursor.execute(
            "UPDATE nightly_benchmarks SET workload_id=%s, optimizer=%s WHERE model_provider_id=%s",
            (workload["id"], Json(optimizer), model["pair_id"]),
        )
        # Link an earlier measured result so the example proves it does not open the latest run.
        example = runs[len(runs) // 2]
        payload = {
            "model_id": model["id"],
            "model_provider_id": model["pair_id"],
            "provider_name": model["provider_name"],
            "status": "interrupted_recovered",
            "successful_configurations": 3,
            "interruption_reason": "The GPU capacity check refused to load the model during restoration.",
            "recovery_resumes": 2,
            "recovery_retry_limit_reached": True,
            **result([example], [{"benchmark_id": example["id"], "status": "success"}]),
        }
        title, body = notification_message(payload)
        event_key = f"docs-benchmark-result:{model['pair_id']}"
        cursor.execute("DELETE FROM notifications WHERE event_key = %s", (event_key,))
        cursor.execute(
            "INSERT INTO notifications(type, title, body, payload, source, event_key) "
            "VALUES ('benchmark_result_ready', %s, %s, %s, 'orchestrator', %s) RETURNING id",
            (title, body, Json(payload), event_key),
        )
        notification_id = cursor.fetchone()["id"]
        cursor.execute(
            "INSERT INTO notification_recipients(notification_id, recipient_kind, recipient_role) "
            "VALUES (%s, 'role', 'logos_admin')",
            (notification_id,),
        )
        if os.environ.get("LOGOS_DOCS_ACTIVE_BENCHMARK") == "true":
            # Status illustration only. A running row does not submit an inference task.
            marker = "docs-performance-status"
            cursor.execute("DELETE FROM jobs WHERE request_payload->>'purpose' = %s", (marker,))
            cursor.execute(
                "INSERT INTO jobs(status, environment, request_payload, result_payload) "
                "VALUES ('running', 'model-provider-benchmark', %s, %s)",
                (
                    Json(
                        {
                            "purpose": marker,
                            "model_provider_id": model["pair_id"],
                            "provider_id": model["provider_id"],
                            "provider_name": model["provider_name"],
                            "model_id": model["id"],
                            "model_name": model["name"],
                            "dataset": "requests/" + workload["id"],
                            "samples": 10,
                            "subset": "main",
                            "split": "test",
                            "max_output_tokens": 512,
                            "batch": {"configurations": [{}, {}], "repetitions": 1},
                        }
                    ),
                    Json(
                        {
                            "stage": "benchmarking",
                            "started_samples": 10,
                            "total_samples": 10,
                            "total_runs": 2,
                            "completed_runs": 0,
                            "run_index": 1,
                        }
                    ),
                ),
            )
        print(json.dumps({"workload": workload["id"], "reference": reference, "illustrative_runs": len(runs)}))
