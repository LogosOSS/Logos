"""Deferred inserts retain event time rather than write-queue execution time."""

import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from logos.dbutils.dbmanager import DBManager


def test_deferred_materialization_preserves_arrival_before_schedule_and_completion():
    arrival = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    scheduled = arrival + datetime.timedelta(seconds=1)
    completed = arrival + datetime.timedelta(seconds=3)
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(id=42)
    db.get_log_id_by_request_id = MagicMock(return_value=None)

    # Materializing long after completion must not date the row after its own events.
    log_id = db.ensure_log_usage(
        api_key_id=1,
        team_id=2,
        user_id=3,
        environment=None,
        log_level="NONE",
        request_id="deferred",
        timestamp_request=arrival,
    )
    assert log_id == 42
    recorded = datetime.datetime.fromisoformat(db.session.execute.call_args.args[1]["ts"])
    assert recorded == arrival
    assert (scheduled - recorded).total_seconds() == 1
    assert (completed - recorded).total_seconds() == 3


def test_immediate_log_call_without_arrival_keeps_current_time_default():
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    db.session.execute.return_value.fetchone.return_value = SimpleNamespace(id=42)
    before = datetime.datetime.now(datetime.timezone.utc)
    db.log_usage(api_key_id=1, team_id=2, user_id=None, environment=None, log_level="NONE")
    recorded = datetime.datetime.fromisoformat(db.session.execute.call_args.args[1]["ts"])
    assert before <= recorded <= datetime.datetime.now(datetime.timezone.utc)
