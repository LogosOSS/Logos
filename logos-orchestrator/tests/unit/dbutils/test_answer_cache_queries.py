"""The queries behind answer reuse (answer_cache, intent_exemplar).

The unit-test environment stubs SQLAlchemy, so ``text()`` yields no
inspectable statement — these assert on the bound parameters, the returned
values and whether the write was committed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from logos import DBManager


def _db():
    db = DBManager.__new__(DBManager)
    db.session = MagicMock()
    return db


def _params_of(call):
    return call.args[1] if len(call.args) > 1 else {}


def _row(**mapping):
    row = MagicMock()
    row._mapping = mapping
    return row


EMBEDDING = [0.5] * 384


def _insert_entry(db, **overrides):
    kwargs = dict(
        context_id="shop-a",
        question_raw="Where is my package {{Order Number}}?",
        question_normalized="Where is my package ?",
        embedding=EMBEDDING,
        intent="track_order",
        answer_template="You can track {{Order Number}} here.",
        source="seed",
    )
    kwargs.update(overrides)
    return db.insert_answer_cache_entry(**kwargs)


def test_cache_entries_are_read_for_one_context_only():
    db = _db()
    db.session.execute.return_value.fetchall.return_value = [
        _row(id=7, intent="track_order", answer_template="a", source="seed", source_model_id=None, embedding=EMBEDDING)
    ]

    entries = db.get_answer_cache_entries("shop-a")

    assert _params_of(db.session.execute.call_args) == {"context_id": "shop-a"}
    assert entries == [
        {
            "id": 7,
            "intent": "track_order",
            "answer_template": "a",
            "source": "seed",
            "source_model_id": None,
            "embedding": EMBEDDING,
        }
    ]


def test_an_empty_context_has_no_entries():
    db = _db()
    db.session.execute.return_value.fetchall.return_value = []

    assert db.get_answer_cache_entries("unknown") == []


def test_intent_exemplars_are_read_for_the_context():
    db = _db()
    db.session.execute.return_value.fetchall.return_value = [
        _row(id=1, intent="track_order", text="where is my package", source="seed", embedding=EMBEDDING)
    ]

    exemplars = db.get_intent_exemplars("shop-a")

    assert _params_of(db.session.execute.call_args) == {"context_id": "shop-a"}
    assert exemplars[0]["intent"] == "track_order"


def test_a_new_answer_is_stored_and_committed():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = _row(id=12)

    entry_id = _insert_entry(db, source="model", source_model_id=3, source_log_id=5013)

    assert entry_id == 12
    params = _params_of(db.session.execute.call_args)
    assert params["context_id"] == "shop-a"
    assert params["question_normalized"] == "Where is my package ?"
    assert params["source"] == "model"
    assert params["source_model_id"] == 3
    assert params["source_log_id"] == 5013
    db.session.commit.assert_called_once()


def test_a_seeded_answer_names_no_model():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = _row(id=1)

    _insert_entry(db)

    params = _params_of(db.session.execute.call_args)
    assert params["source"] == "seed"
    assert params["source_model_id"] is None
    assert params["source_log_id"] is None


def test_an_already_stored_question_returns_none():
    # ON CONFLICT DO NOTHING returns no row, so loading the same data twice
    # inserts nothing the second time.
    db = _db()
    db.session.execute.return_value.fetchone.return_value = None

    assert _insert_entry(db) is None


def test_the_embedding_is_bound_as_plain_floats():
    # A NumPy vector cannot be bound by the database driver; the method
    # converts every value to a Python float.
    db = _db()
    db.session.execute.return_value.fetchone.return_value = _row(id=1)

    class NumpyLikeFloat(float):
        pass

    _insert_entry(db, embedding=[NumpyLikeFloat(0.25)] * 384)

    bound = _params_of(db.session.execute.call_args)["embedding"]
    assert len(bound) == 384
    assert all(type(value) is float for value in bound)


def test_a_global_intent_exemplar_has_no_context():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = _row(id=4)

    exemplar_id = db.insert_intent_exemplar(
        context_id=None, intent="track_order", text_value="where is my package", embedding=EMBEDDING
    )

    assert exemplar_id == 4
    params = _params_of(db.session.execute.call_args)
    assert params["context_id"] is None
    assert params["source"] == "seed"
    db.session.commit.assert_called_once()


def test_a_duplicate_intent_exemplar_returns_none():
    db = _db()
    db.session.execute.return_value.fetchone.return_value = None

    assert db.insert_intent_exemplar(context_id="shop-a", intent="x", text_value="t", embedding=EMBEDDING) is None


def test_a_cache_hit_is_counted_on_its_entry():
    db = _db()

    db.record_answer_cache_hit(7)

    assert _params_of(db.session.execute.call_args) == {"entry_id": 7}
    db.session.commit.assert_called_once()


def test_the_cache_outcome_can_be_written_to_the_log_row():
    db = _db()

    db.update_log_entry_metrics(request_id="req-1", cache_hit=True, cache_entry_id=7)

    written = [_params_of(call) for call in db.session.execute.call_args_list]
    assert any(params.get("cache_hit") is True and params.get("cache_entry_id") == 7 for params in written)


def test_a_cache_miss_is_written_as_false_not_dropped():
    # The metrics update drops None fields; FALSE ("looked, found nothing")
    # must still reach the row, or misses would be indistinguishable from
    # requests the cache never saw.
    db = _db()

    db.update_log_entry_metrics(request_id="req-2", cache_hit=False)

    written = [_params_of(call) for call in db.session.execute.call_args_list]
    assert any(params.get("cache_hit") is False for params in written)


def test_a_cache_hit_settles_its_log_row_and_counts_the_hit_in_one_commit():
    db = _db()

    db.finalize_cache_hit_row(55, 7, request_id="req-1")

    log_params, hit_params = (_params_of(call) for call in db.session.execute.call_args_list)
    assert log_params["log_id"] == 55
    assert log_params["entry_id"] == 7
    assert log_params["request_id"] == "req-1"
    assert log_params["timestamp"] is not None
    assert hit_params == {"entry_id": 7}
    db.session.commit.assert_called_once()


def test_a_cache_hit_is_settled_at_zero_cost_and_finalised(monkeypatch):
    # Budget reads take a finalised row's settled cost as it stands; an
    # unfinalised one is priced again from its usage on every read.
    from logos.dbutils import dbmanager

    monkeypatch.setattr(dbmanager, "text", lambda sql: sql)  # keep the statement inspectable
    db = _db()

    db.finalize_cache_hit_row(55, 7)

    log_update = " ".join(str(db.session.execute.call_args_list[0].args[0]).split())
    assert "settled_cost_micro_cents = 0" in log_update
    assert "cost_finalized = TRUE" in log_update


def test_a_pending_log_reference_is_not_written_as_a_row():
    db = _db()

    db.finalize_cache_hit_row("pending", 7)

    db.session.execute.assert_not_called()
    db.session.commit.assert_not_called()
