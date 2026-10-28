"""Content-addressed, private request snapshots from explicitly selected FULL logs."""

import copy
import hashlib
import json

from sqlalchemy import text

MAX_REQUESTS = 100
MAX_BYTES = 5_000_000
BODY_FIELDS = {
    "messages",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "response_format",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "seed",
    "stop",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "logit_bias",
    "reasoning_effort",
    "chat_template_kwargs",
}
IGNORED_FIELDS = {"model", "stream", "stream_options", "user", "metadata", "headers", "api_key", "authorization"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def request_body(payload):
    """Copy inference controls without identities, headers, credentials or transport settings."""
    if not isinstance(payload, dict):
        raise ValueError("Every source request must contain a JSON object")
    if set(payload) - BODY_FIELDS - IGNORED_FIELDS:
        raise ValueError("A source request has unsupported inference controls")
    body = {key: copy.deepcopy(value) for key, value in payload.items() if key in BODY_FIELDS}
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Every source request must contain messages")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "system",
            "developer",
            "user",
            "assistant",
            "tool",
        }:
            raise ValueError("Unsupported message role")
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(block, dict) or block.get("type") != "text" for block in content):
                raise ValueError("Request snapshots currently support text content only")
        elif content is not None and not isinstance(content, str):
            raise ValueError("Unsupported message content")
    return body


def build_snapshot(rows, name, provenance):
    """Validate the entire selection; missing, failed or non-FULL rows never disappear silently."""
    if not 1 <= len(rows) <= MAX_REQUESTS:
        raise ValueError("Select between 1 and 100 requests")
    if provenance not in {"synthetic", "production"}:
        raise ValueError("Declare synthetic or production provenance")
    if not isinstance(name, str) or not name.strip() or len(name) > 128:
        raise ValueError("A workload name of at most 128 characters is required")
    model_ids = {row["model_id"] for row in rows}
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Select distinct source requests")
    if len(model_ids) != 1 or None in model_ids:
        raise ValueError("Source requests must resolve to one model")
    for row in rows:
        if str(row["privacy_level"]) != "FULL" or str(row["result_status"]) != "success":
            raise ValueError("Only successful FULL requests can enter a snapshot")
        if row.get("environment") == "model-provider-benchmark":
            raise ValueError("Benchmark replays cannot become source requests")
    requests = [{"source_log_id": row["id"], "body": request_body(row["input_payload"])} for row in rows]
    content = {"model_id": rows[0]["model_id"], "provenance": provenance, "requests": requests}
    encoded = canonical(content)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError("The snapshot exceeds 5 MB")
    return {
        "id": hashlib.sha256(encoded.encode()).hexdigest(),
        "name": name.strip(),
        **content,
        "model_name": rows[0]["model_name"],
        "request_count": len(requests),
    }


def metadata(snapshot):
    return {key: snapshot[key] for key in ("id", "name", "model_id", "model_name", "provenance", "request_count")}


def create(log_ids, name, provenance):
    """Freeze an ordered, explicit selection. No source headers or response bodies are copied."""
    from logos.dbutils.dbmanager import DBManager

    if not log_ids or len(log_ids) > MAX_REQUESTS or len(set(log_ids)) != len(log_ids):
        raise ValueError("Select 1 to 100 distinct request IDs")
    with DBManager() as db:
        rows = list(
            db.session.execute(
                text(
                    "SELECT le.id, le.model_id, m.name AS model_name, le.privacy_level::text, "
                    "le.result_status::text, le.environment, le.input_payload FROM log_entry le "
                    "LEFT JOIN models m ON m.id = le.model_id WHERE le.id = ANY(:ids) "
                    "ORDER BY array_position(:ids, le.id)"
                ),
                {"ids": log_ids},
            ).mappings()
        )
        if len(rows) != len(log_ids):
            raise ValueError("A selected source request no longer exists")
        snapshot = build_snapshot(rows, name, provenance)
        db.session.execute(
            text(
                "INSERT INTO benchmark_workloads (id, name, model_id, model_name, provenance, requests, request_count) "
                "VALUES (:id, :name, :model_id, :model_name, :provenance, CAST(:requests AS jsonb), :request_count) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {**metadata(snapshot), "requests": canonical(snapshot["requests"])},
        )
        db.session.commit()
    return metadata(snapshot)


def load(workload_id, model_id=None):
    from logos.dbutils.dbmanager import DBManager

    with DBManager() as db:
        row = (
            db.session.execute(text("SELECT * FROM benchmark_workloads WHERE id = :id"), {"id": workload_id})
            .mappings()
            .first()
        )
    if row is None or (model_id is not None and row["model_id"] != model_id):
        raise ValueError("Request snapshot is unavailable for this model")
    snapshot = dict(row)
    content = {key: snapshot[key] for key in ("model_id", "provenance", "requests")}
    if hashlib.sha256(canonical(content).encode()).hexdigest() != workload_id:
        raise ValueError("Request snapshot checksum does not match")
    if len(snapshot["requests"]) != snapshot["request_count"]:
        raise ValueError("Request snapshot count does not match")
    return snapshot


def list_for_model(model_id):
    from logos.dbutils.dbmanager import DBManager

    with DBManager() as db:
        return [
            dict(row)
            for row in db.session.execute(
                text(
                    "SELECT id, name, model_id, model_name, provenance, request_count FROM benchmark_workloads "
                    "WHERE model_id = :model_id ORDER BY created_at DESC LIMIT 50"
                ),
                {"model_id": model_id},
            ).mappings()
        ]
