import json
from contextlib import nullcontext
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from logos.benchmarks import nightly_store as store
from logos.benchmarks.completion import notification_message

REASON = "Could not make enough GPU capacity available to load the model."
REPORT = [{"status": "success"}, {"status": "recovery_required", "reason": REASON}]


@pytest.fixture
def nightly(monkeypatch):
    row = {
        "last_run_date": date(2026, 10, 29),
        "status": "recovery_required",
        "optimizer": {},
        "enabled": True,
        "model_id": 11,
        "provider_name": "Test worker",
    }
    events = []

    def execute(_, params):
        if "state" in params:
            row.update(status=params["state"], optimizer=json.loads(params["optimizer"]))
        elif "event" in params:
            events.append(json.loads(params["payload"]))
        else:
            return Mock(mappings=lambda: Mock(one=lambda: dict(row)))

    db = Mock()
    db.session.execute.side_effect = execute
    monkeypatch.setattr(store, "DBManager", lambda: nullcontext(db))
    return row, events


def test_recovery_budget_survives_a_fresh_search_and_resets_the_next_night(nightly):
    row, events = nightly
    for attempt in range(store.MAX_RECOVERY_RESUMES + 1):
        row["status"] = "recovery_required"
        store.finish(
            31, "interrupted_recovered", REPORT, {"state": {}, "reference_id": None}, resume_after_recovery=True
        )
        if attempt < store.MAX_RECOVERY_RESUMES:
            assert row["status"] == "continuing"
            assert not events

    assert row["status"] == "interrupted_recovered"
    (event,) = events
    assert event["recovery_resumes"] == 2
    assert event["rounds"] == event["successful_configurations"] == 3
    assert event["recovery_retry_limit_reached"]
    assert event["interruption_reason"] == REASON
    title, body = notification_message(event)
    assert title == "Nightly benchmark interrupted"
    assert REASON in body and "The nightly retry limit was reached." in body

    row.update(last_run_date=row["last_run_date"] + timedelta(days=1), status="recovery_required")
    store.finish(31, "interrupted_recovered", REPORT, resume_after_recovery=True)
    assert row["status"] == "continuing"
    assert row["optimizer"]["nightly_session"]["recovery_resumes"] == 1
    assert len(events) == 1


@pytest.mark.parametrize("enabled,can_resume", [(False, True), (True, False)])
def test_confirmed_recovery_stays_final_if_disabled_or_outside_the_window(nightly, enabled, can_resume):
    row, events = nightly
    row["enabled"] = enabled
    store.finish(31, "interrupted_recovered", [], resume_after_recovery=can_resume)
    assert row["status"] == "interrupted_recovered"
    (event,) = events
    assert event["recovery_resumes"] == 0
    assert "before worker restoration was confirmed" in event["interruption_reason"]


def test_completed_night_keeps_the_interruption_and_measurement_from_an_earlier_round(nightly):
    row, events = nightly
    store.finish(31, "interrupted_recovered", REPORT, resume_after_recovery=True)
    row["optimizer"]["last_round_result"] = {"benchmark_id": 135}
    row["status"] = "running"
    store.finish(31, "completed", [], {"state": {}, "reference_id": None})
    (event,) = events
    assert event["interruption_reason"] == REASON
    assert event["benchmark_id"] == 135
    assert event["recovery_resumes"] == 1
    title, body = notification_message(event)
    assert title == "Nightly benchmark finished"
    assert "Benchmark #135" in body and REASON in body


@pytest.mark.parametrize(
    ("failed_adoption", "target"),
    [
        (False, None),
        (True, None),
        (True, {"model_id": 11, "provider_name": "Test worker"}),
    ],
)
def test_save_queues_the_failed_candidate_with_its_rollback(monkeypatch, failed_adoption, target):
    failure = {
        "status": "adoption_failed",
        "benchmark_id": 164,
        "baseline_id": 160,
        "parameters": {"max_num_batched_tokens": 2304},
        "throughput": 32.8,
        "baseline_throughput": 22.92,
        "reason": REASON,
    }
    db = Mock()
    db.session.execute.return_value.mappings.return_value.first.return_value = target
    monkeypatch.setattr(store, "DBManager", lambda: nullcontext(db))

    journal = {"provider_id": 4}
    report = [failure] if failed_adoption else []
    store.save(31, "running", report, journal, adoption_failure=failure if failed_adoption else None)

    calls = db.session.execute.call_args_list
    assert json.loads(calls[0].args[1]["recovery"]) == journal
    assert json.loads(calls[0].args[1]["report"]) == report
    db.session.commit.assert_called_once_with()
    if target is None:
        assert len(calls) == (2 if failed_adoption else 1)
        return

    assert len(calls) == 3
    outbox = calls[2].args[1]
    assert outbox["event"] == "nightly-adoption-failed:31:164"
    payload = json.loads(outbox["payload"])
    assert payload == {**failure, **target, "model_provider_id": 31}
    title, body = notification_message(payload)
    assert title == "Nightly configuration adoption failed"
    assert "Test worker · Benchmark #164 · Max batched tokens 2304" in body
    assert "32.8 tok/s (+43.1% vs. baseline #160)" in body
    assert REASON in body
