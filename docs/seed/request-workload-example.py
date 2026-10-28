"""Illustrative request workload for the isolated documentation stack; no GPU measurements."""

import json
import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from logos.benchmarks.optimization import context
from logos.benchmarks.workloads import build_snapshot

if os.environ.get("LOGOS_DOCS_ENABLED") != "true":
    raise SystemExit("Run only in the isolated documentation orchestrator")

with psycopg2.connect(os.environ["LOGOS_DB_URL"]) as db:
    with db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT mp.id AS pair_id, m.id, m.name FROM model_provider mp "
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
            "INSERT INTO benchmark_workloads(id, name, model_id, model_name, provenance, requests, request_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, 10) ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name",
            (workload["id"], workload["name"], model["id"], model["name"], "synthetic", Json(workload["requests"])),
        )
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
                tool="logos-replay",
                workload={key: workload[key] for key in ("id", "name", "provenance", "request_count")},
            )
            nightly = configuration["nightly"]
            nightly.update(
                reference_id=reference, series_key=series, workload_id=workload["id"], eligible_for_selection=True
            )
            configuration["serving"] = {**production, **nightly.get("parameters", {})}
            metrics = run["metrics"]
            rate = (
                metrics.get("aggregate_output_tokens_per_second")
                or metrics["output_tokens_per_second"]["successful"]["mean"]
            )
            metrics["aggregate_output_tokens_per_second"] = rate
            metrics["request_totals"] = {"total": 10, "successful": 10, "errored": 0, "incomplete": 0}
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
            "best_parameters": {"max_num_seqs": 20, "max_num_batched_tokens": 4096},
            "iterations": 3,
        }
        cursor.execute(
            "UPDATE nightly_benchmarks SET workload_id=%s, optimizer=%s WHERE model_provider_id=%s",
            (workload["id"], Json(optimizer), model["pair_id"]),
        )
        print(json.dumps({"workload": workload["id"], "reference": reference, "illustrative_runs": len(runs)}))
