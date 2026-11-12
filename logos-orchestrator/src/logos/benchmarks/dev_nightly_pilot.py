"""Install the synthetic coding conversations on the Dev nightly schedule."""

import json
from pathlib import Path

from sqlalchemy import text

from logos.benchmarks import nightly_store, recent_requests, workloads
from logos.dbutils.dbmanager import DBManager

MODEL_NAME = "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ"
WORKER_NAME = "Hochbruegge VM2"
PREVIOUS_WORKLOAD_IDS = {
    "c42aaea0ddc7f578082cf43683fca8bd61b0b259492ad0070a47064b75517671",
    "c8ae56bf8f9d348214a9a7e993aa085f7ed113db7aed4824c55052c5b8260529",
    "5fe0f1924cff8ac2d7c585af59c66e829da7229a6bc8a9bc5c17f1bfe6cf3a9c",
}
NAME = "Qwen coding pilot · 50 realistic coding conversations · v3"
WORKLOAD_PATH = Path(__file__).with_name("dev_coding_workload.json")


def build_workload(model_id):
    """Freeze supplied code, incident context and follow-ups without real production data."""
    rows = [
        {
            "id": request["id"],
            "model_id": model_id,
            "model_name": MODEL_NAME,
            "privacy_level": "FULL",
            "result_status": "success",
            "input_payload": request["body"],
        }
        for request in json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
    ]
    return workloads.build_snapshot(rows, NAME, "synthetic")


def install():
    if recent_requests.username():
        raise RuntimeError("The Dev pilot requires manually selected nightly requests")
    with DBManager() as db:
        target = db.session.execute(
            text(
                "SELECT mp.id AS pair_id, m.id AS model_id FROM model_provider mp "
                "JOIN models m ON m.id = mp.model_id JOIN providers p ON p.id = mp.provider_id "
                "WHERE m.name = :model AND p.name = :worker"
            ),
            {"model": MODEL_NAME, "worker": WORKER_NAME},
        ).one()
    pair_id = target.pair_id
    snapshot = build_workload(target.model_id)
    schedule = nightly_store.status(pair_id)
    if not schedule["enabled"] or schedule["workload_id"] not in PREVIOUS_WORKLOAD_IDS | {snapshot["id"]}:
        raise RuntimeError("The expected enabled Dev pilot schedule has changed")
    with DBManager() as db:
        workloads.save_snapshot(db, snapshot)
        db.session.commit()
    stored = workloads.load(snapshot["id"], target.model_id)
    if schedule["workload_id"] != snapshot["id"]:
        schedule = nightly_store.set_enabled(
            pair_id, True, expected=True, workload_id=snapshot["id"], expected_workload_id=schedule["workload_id"]
        )
    else:
        schedule = nightly_store.status(pair_id)
    if not schedule["enabled"] or schedule["workload_id"] != stored["id"]:
        raise RuntimeError("The natural-output workload is not selected on the Dev schedule")
    print(
        f"Dev nightly pair {pair_id}: {stored['request_count']} requests with natural output lengths; "
        f"workload {stored['id']} ({schedule['status']})"
    )


if __name__ == "__main__":
    install()
