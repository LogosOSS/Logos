"""Answer reuse inside route_and_execute: a hit returns before any scheduling.

The cache, the persistence of a hit and both execution modes are replaced
by recorders, so each test sees exactly which path a request took.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import logos as main
from logos.answer_reuse.matching import Match
from logos.answer_reuse.service import CACHE_MODEL_NAME, CachedAnswer

DEPLOYMENTS = [{"model_id": 1, "provider_id": 1}]
QUESTION = {"messages": [{"role": "user", "content": "Where is my package?"}]}
CONTEXT = {"logos-context": "shop-a"}

ANSWER = CachedAnswer(
    entry_id=7,
    answer="You can track {{Order Number}} here.",
    intent="track_order",
    similarity=0.93,
    source="model",
    source_model_id=4,
)
HIT = (ANSWER, Match(accepted=True, index=0, similarity=0.93, label="track_order"))
MISS = (None, Match(accepted=False, index=0, similarity=0.62, label="track_order"))


class _Recorder:
    """Stands in for one execution path and records whether it ran."""

    def __init__(self, name):
        self.name = name
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        return {"status": self.name}


@pytest.fixture
def paths(monkeypatch):
    proxy, resource = _Recorder("proxy"), _Recorder("resource")
    monkeypatch.setattr(main, "_execute_proxy_mode", proxy)
    monkeypatch.setattr(main, "_execute_resource_mode", resource)
    return SimpleNamespace(proxy=proxy, resource=resource)


@pytest.fixture
def pipeline(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(main, "_pipeline", fake, raising=False)
    return fake


@pytest.fixture
def persisted(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_persist_cache_hit", lambda *args: calls.append(args))
    return calls


def _use_cache(monkeypatch, result=None, error=None):
    cache = MagicMock()
    if error is not None:
        cache.find.side_effect = error
    else:
        cache.find.return_value = result
    monkeypatch.setattr(main, "_get_answer_cache", lambda: cache)
    return cache


async def _route(body=QUESTION, headers=CONTEXT, is_async_job=False, **kwargs):
    return await main.route_and_execute(
        deployments=DEPLOYMENTS,
        body=body,
        headers=headers,
        auth=MagicMock(team_id=3, api_key_id=10),
        path="chat/completions",
        log_id=kwargs.get("log_id", 55),
        is_async_job=is_async_job,
        request_id="req-1",
    )


async def test_a_hit_never_reaches_classification_scheduling_or_a_model(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, HIT)

    response = await _route()

    assert paths.resource.calls == 0
    assert paths.proxy.calls == 0
    assert pipeline.process.call_count == 0
    assert pipeline.scheduler.method_calls == []
    assert response.status_code == 200


async def test_a_hit_returns_the_stored_answer_as_a_chat_completion(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, HIT)

    response = await _route()

    body = response.body.decode()
    assert '"content":"You can track {{Order Number}} here."' in body
    assert f'"model":"{CACHE_MODEL_NAME}"' in body
    assert response.headers["X-Logos-Cache"] == "hit"
    assert response.headers["X-Logos-Cache-Entry"] == "7"
    assert response.headers["X-Logos-Cache-Source-Model"] == "4"
    assert response.headers["X-Request-ID"] == "req-1"


async def test_a_request_naming_another_model_can_still_be_answered(monkeypatch, paths, pipeline, persisted):
    # A cached answer may serve a request that named a different model; the
    # response reports the model that wrote it.
    _use_cache(monkeypatch, HIT)

    response = await _route(body={**QUESTION, "model": "local-llama"})

    assert paths.proxy.calls == 0
    assert response.headers["X-Logos-Cache-Source-Model"] == "4"


async def test_a_hit_is_settled_with_its_log_row_entry_and_payload(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, HIT)

    await _route(log_id=55)

    assert len(persisted) == 1
    log_id, request_id, entry_id, payload = persisted[0]
    assert (log_id, request_id, entry_id) == (55, "req-1", 7)
    assert payload["choices"][0]["message"]["content"] == ANSWER.answer


async def test_a_hit_in_a_job_returns_the_job_result_shape(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, HIT)

    result = await _route(is_async_job=True)

    assert result["status_code"] == 200
    assert result["data"]["logos"]["answer_reuse"]["entry_id"] == 7


async def test_a_miss_takes_the_normal_path_and_is_recorded(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, MISS)

    out = await _route()

    assert out == {"status": "resource"}
    pipeline.record_answer_reuse_miss.assert_called_once_with("req-1")
    assert persisted == []


async def test_without_the_header_the_cache_is_never_consulted(monkeypatch, paths, pipeline, persisted):
    cache = _use_cache(monkeypatch, HIT)

    out = await _route(headers={"Authorization": "Bearer x"})

    assert out == {"status": "resource"}
    cache.find.assert_not_called()
    pipeline.record_answer_reuse_miss.assert_not_called()


async def test_an_ineligible_request_never_consults_the_cache(monkeypatch, paths, pipeline, persisted):
    cache = _use_cache(monkeypatch, HIT)

    out = await _route(body={**QUESTION, "stream": False, "tools": [{"type": "function"}]})

    assert out == {"status": "resource"}
    cache.find.assert_not_called()


async def test_an_unusable_header_is_a_400_before_any_work(monkeypatch, paths, pipeline, persisted):
    cache = _use_cache(monkeypatch, HIT)

    with pytest.raises(main.HTTPException) as exc:
        await _route(headers={"logos-context": "bad\nvalue"})

    assert exc.value.status_code == 400
    assert paths.resource.calls == 0
    cache.find.assert_not_called()


async def test_an_unusable_header_in_a_job_is_a_400_result(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, HIT)

    result = await _route(headers={"logos-context": "x" * 500}, is_async_job=True)

    assert result["status_code"] == 400


async def test_a_failing_lookup_falls_back_to_the_normal_path(monkeypatch, paths, pipeline, persisted):
    _use_cache(monkeypatch, error=RuntimeError("database down"))

    out = await _route()

    assert out == {"status": "resource"}
    assert persisted == []
    pipeline.record_answer_reuse_miss.assert_not_called()


# settling a hit


class _FakeDB:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return record


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(main, "DBManager", lambda: db)
    return db


def test_settling_a_hit_writes_the_row_and_the_payload(fake_db, pipeline):
    pipeline.take_monitoring_buffer.return_value = {"cache_hit": None, "queue_depth_at_arrival": 2}

    main._persist_cache_hit(55, "req-1", 7, {"x": 1})

    names = [name for name, _, _ in fake_db.calls]
    assert names == ["finalize_cache_hit_row", "store_response_payload", "update_log_entry_metrics"]
    assert fake_db.calls[0][1] == (55, 7)
    assert fake_db.calls[0][2] == {"request_id": "req-1"}
    assert fake_db.calls[1][1] == (55, {"x": 1})
    assert fake_db.calls[2][2] == {"log_id": 55, "request_id": "req-1", "queue_depth_at_arrival": 2}
    pipeline.discard_request.assert_called_once_with("req-1", "success")


def test_settling_a_hit_without_a_log_row_still_counts_the_hit(fake_db, pipeline):
    pipeline.take_monitoring_buffer.return_value = {}

    main._persist_cache_hit(None, "req-1", 7, {"x": 1})

    assert [name for name, _, _ in fake_db.calls] == ["record_answer_cache_hit"]
    assert fake_db.calls[0][1] == (7,)


def test_a_failing_settlement_never_reaches_the_client(monkeypatch, pipeline):
    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(main, "DBManager", broken)

    main._persist_cache_hit(55, "req-1", 7, {"x": 1})


def test_settling_survives_a_failing_monitoring_buffer(fake_db, pipeline):
    pipeline.take_monitoring_buffer.side_effect = RuntimeError("buffer gone")

    main._persist_cache_hit(55, "req-1", 7, {"x": 1})

    assert [name for name, _, _ in fake_db.calls] == ["finalize_cache_hit_row", "store_response_payload"]


# the wiring behind the cache


def test_the_cache_is_built_once(monkeypatch):
    monkeypatch.setattr(main, "_answer_cache", None)

    first = main._get_answer_cache()

    assert main._get_answer_cache() is first


def test_stored_answers_are_read_through_the_database(fake_db):
    main._load_answer_cache_entries("team:3:shop-a")

    assert fake_db.calls == [("get_answer_cache_entries", ("team:3:shop-a",), {})]


def test_questions_are_embedded_with_the_classifier_model(pipeline):
    pipeline.classifier.laura.model.encode.return_value = [0.1, 0.2]

    assert main._encode_for_answer_reuse("query: hi") == [0.1, 0.2]
    pipeline.classifier.laura.model.encode.assert_called_once_with(
        "query: hi", normalize_embeddings=True, show_progress_bar=False
    )


def test_embedding_before_the_pipeline_starts_is_an_error(monkeypatch):
    monkeypatch.setattr(main, "_pipeline", None, raising=False)

    with pytest.raises(RuntimeError):
        main._encode_for_answer_reuse("query: hi")


def test_a_miss_without_a_request_id_or_pipeline_records_nothing(monkeypatch, pipeline):
    main._record_answer_reuse_miss(None)
    pipeline.record_answer_reuse_miss.assert_not_called()

    monkeypatch.setattr(main, "_pipeline", None, raising=False)
    main._record_answer_reuse_miss("req-1")


def test_a_failing_miss_record_never_reaches_the_client(pipeline):
    pipeline.record_answer_reuse_miss.side_effect = RuntimeError("monitoring down")

    main._record_answer_reuse_miss("req-1")
