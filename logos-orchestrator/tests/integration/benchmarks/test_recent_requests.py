import json
from datetime import datetime, timedelta

import pytest
import test_nightly_store
from sqlalchemy import text

from logos.benchmarks import recent_requests

database = test_nightly_store.database

pytestmark = pytest.mark.integration
START = datetime.fromisoformat("2026-10-29T05:00:00+01:00")


def seed(engine, count=55):
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users VALUES (1, 'source.user'), (2, 'another.user')"))
        connection.execute(text("INSERT INTO nightly_benchmarks(model_provider_id, enabled) VALUES (31, true)"))
        for identifier in range(1, count + 1):
            connection.execute(
                text(
                    "INSERT INTO log_entry(id, user_id, model_id, privacy_level, result_status, input_payload, "
                    "timestamp_request, timestamp_response) VALUES (:id, 1, :model, 'FULL', 'success', "
                    '\'{"messages":[{"role":"user","content":"Synthetic request"}]}\', :time, :time)'
                ),
                {
                    "id": identifier,
                    "model": 11 if identifier % 2 else 12,
                    "time": START - timedelta(minutes=100 - identifier),
                },
            )


def test_latest_fifty_are_exactly_once_shared_across_models_and_survive_cleanup(database, monkeypatch):
    engine, _ = database
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    seed(engine)
    first = recent_requests.freeze(START)
    assert first["user_id"] == 1
    assert [row["source_log_id"] for row in first["requests"]] == list(range(55, 5, -1))
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM log_entry"))
    second = recent_requests.freeze(START + timedelta(hours=1))
    assert second["requests"] == first["requests"]
    model_a = recent_requests.for_model({"model_id": 11, "model_name": "model-a"}, START)
    model_b = recent_requests.for_model({"model_id": 12, "model_name": "model-b"}, START)
    assert model_a["requests"] == model_b["requests"]
    assert model_a["id"] != model_b["id"]
    assert datetime.fromisoformat(model_a["source_selection"]["cutoff"]) == START


@pytest.mark.parametrize(
    "field, value",
    [
        ("user_id", "2"),
        ("privacy_level", "'BILLING'"),
        ("result_status", "'failed'"),
        ("environment", "'model-provider-benchmark'"),
        ("timestamp_response", "'2026-10-29T05:01:00+01:00'"),
        ("input_payload", "NULL"),
    ],
)
def test_wrong_owner_non_full_failed_replay_and_late_requests_do_not_enter_selection(
    database, monkeypatch, field, value
):
    engine, _ = database
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    seed(engine, count=50)
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE log_entry SET {field} = {value} WHERE id = 50"))
    selected = recent_requests.freeze(START)
    assert selected["requests"] is None
    assert "49 of 50" in selected["error"]
    with pytest.raises(ValueError, match="49 of 50"):
        recent_requests.for_model({"model_id": 11, "model_name": "model-a"}, START)


def test_cutoff_is_the_window_start_even_after_restart_and_failure_stays_frozen(database, monkeypatch):
    engine, _ = database
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    seed(engine, count=49)
    first = recent_requests.freeze(START + timedelta(hours=1))
    assert "49 of 50" in first["error"]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE log_entry SET timestamp_request = :late, timestamp_response = :late WHERE id = 49"),
            {"late": START + timedelta(minutes=1)},
        )
    assert recent_requests.freeze(START + timedelta(hours=2))["error"] == first["error"]


def test_selected_mode_and_outside_window_do_not_freeze(database, monkeypatch):
    monkeypatch.delenv("LOGOS_NIGHTLY_REQUEST_USERNAME", raising=False)
    assert recent_requests.freeze(START) is None
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    assert recent_requests.freeze(START - timedelta(seconds=1)) is None
    assert recent_requests.freeze(START + timedelta(hours=3)) is None


def test_unsupported_inputs_block_the_cohort_without_exposing_prompts_in_policy(database, monkeypatch):
    engine, _ = database
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    seed(engine, count=50)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE log_entry SET input_payload = input_payload || CAST(:extra AS jsonb) WHERE id=50"),
            {"extra": json.dumps({"n": 2})},
        )
    assert "unsupported" in recent_requests.freeze(START)["error"]
    info = recent_requests.policy(START)
    assert info["ready"] is False
    assert "requests" not in info


def test_mixed_native_messages_and_chat_requests_keep_their_protocol_and_controls(database, monkeypatch):
    engine, _ = database
    monkeypatch.setenv("LOGOS_NIGHTLY_REQUEST_USERNAME", "source.user")
    seed(engine, count=50)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE log_entry SET input_payload = input_payload || CAST(:extra AS jsonb) WHERE id=50"),
            {"extra": json.dumps({"system": "Synthetic system", "thinking": {"type": "adaptive"}, "max_tokens": 8192})},
        )
    frozen = recent_requests.freeze(START)
    assert frozen["error"] is None
    assert frozen["requests"][0]["operation"] == "messages"
    assert frozen["requests"][0]["body"]["thinking"] == {"type": "adaptive"}
    assert frozen["requests"][0]["body"]["max_tokens"] == 8192
    assert frozen["requests"][1]["operation"] == "chat/completions"
