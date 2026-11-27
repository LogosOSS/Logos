"""Verify admission and settlement at the ledger boundary, without upstream traffic."""

import json
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from logos.billing import provider_spend


@pytest.fixture
def ledger(monkeypatch):
    session = MagicMock()
    database = MagicMock()
    database.__enter__.return_value.session = session
    monkeypatch.setattr(provider_spend, "DBManager", lambda: database)
    monkeypatch.setattr(provider_spend, "text", lambda sql: sql)
    return session


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.openai.com/v1/responses", "responses"),
        ("https://api.openai.com:443/v1/chat/completions", "chat"),
        ("https://api.anthropic.com/v1/messages", "messages"),
        ("https://api.openai.com/v1/embeddings", None),
        ("http://api.openai.com/v1/responses", None),
        ("https://api.openai.com:444/v1/responses", None),
        ("https://api.openai.com:invalid/v1/responses", None),
        ("https://api.openai.com/v1/responses?option=1", None),
        ("https://api.anthropic.com/v1/messages#fragment", None),
        ("https://examples.invalid/v1/messages", None),
    ],
)
def test_reserves_only_supported_protocols(ledger, url, expected):
    ledger.execute.return_value.scalar_one.return_value = "reserved" if expected else "uncapped"
    payload = {"model": "example", "max_tokens": 8}
    result = provider_spend.reserve(22, 33, url=url, payload=payload)
    _, bound = ledger.execute.call_args.args
    assert (bound["provider"], bound["model"], bound["kind"]) == (22, 33, expected)
    if expected:
        assert isinstance(result, uuid.UUID)
        assert result == bound["attempt"]
        assert json.loads(bound["payload"]) == payload
    else:
        assert result is None
        assert bound["payload"] is None
    ledger.commit.assert_called_once()


@pytest.mark.parametrize(("verdict", "status"), [("exhausted", 402), ("unbounded", 422), ("unknown", 503)])
def test_admission_denies_unconfirmed_or_over_budget_attempts(ledger, verdict, status):
    ledger.execute.return_value.scalar_one.return_value = verdict
    with pytest.raises(HTTPException) as exc:
        provider_spend.reserve(22)
    assert exc.value.status_code == status


def test_admission_denies_when_ledger_cannot_commit(ledger):
    ledger.execute.return_value.scalar_one.return_value = "reserved"
    ledger.commit.side_effect = RuntimeError("unavailable")
    with pytest.raises(HTTPException) as exc:
        provider_spend.reserve(22)
    assert exc.value.status_code == 503


def test_settlement_requires_complete_reported_usage_and_preserves_the_tier(ledger):
    reported = {"prompt_tokens": 5, "completion_tokens": 3}
    attempt = uuid.uuid4()
    provider_spend.settle(None, reported, "flex")
    provider_spend.settle(attempt, {"usage_estimated": 1, **reported}, "flex")
    provider_spend.settle(attempt, {"prompt_tokens": 5}, "flex")
    provider_spend.settle(attempt, {"prompt_tokens": True, "completion_tokens": 3}, "flex")
    ledger.execute.assert_not_called()

    provider_spend.settle(attempt, reported, "flex")
    statement, bound = ledger.execute.call_args.args
    assert "logos_settle_provider_spend" in str(statement)
    assert bound["attempt"] == attempt
    assert bound["tier"] == "flex"
    assert json.loads(bound["usage"]) == reported
    ledger.commit.assert_called_once()


def test_release_requires_explicit_no_dispatch_and_survives_ledger_failure(ledger, caplog):
    attempt = uuid.uuid4()
    provider_spend.release_undispatched(None)
    ledger.execute.assert_not_called()

    provider_spend.release_undispatched(attempt)
    statement, bound = ledger.execute.call_args.args
    assert "logos_settle_provider_spend(:attempt, NULL, NULL, true)" in str(statement)
    assert bound == {"attempt": attempt}
    ledger.commit.assert_called_once()

    ledger.execute.side_effect = RuntimeError("unavailable")
    provider_spend.release_undispatched(attempt)
    provider_spend.settle(attempt, {"prompt_tokens": 5, "completion_tokens": 3}, None)
    assert "could not release" in caplog.text
    assert "keeping reservation" in caplog.text
