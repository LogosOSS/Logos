"""Which requests take part in answer reuse, the in-memory cache, and the cached response."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from logos.answer_reuse import service
from logos.answer_reuse.context import InvalidContextId
from logos.answer_reuse.service import (
    CACHE_MODEL_NAME,
    AnswerCache,
    CachedAnswer,
    ReuseRequest,
    cache_hit_headers,
    chat_completion_payload,
    reuse_request_from,
)

CONTEXT = {"logos-context": "shop-a"}


def _body(*messages, **extra):
    return {"messages": list(messages) or [{"role": "user", "content": "Where is my package?"}], **extra}


def _eligible(body=None, headers=CONTEXT, path="chat/completions", team_id=3, api_key_id=10):
    return reuse_request_from(body if body is not None else _body(), headers, path, team_id, api_key_id)


# which requests are eligible


def test_a_plain_question_with_a_context_is_eligible():
    assert _eligible() == ReuseRequest(scope="team:3:shop-a", question="Where is my package?")


def test_a_key_without_a_team_scopes_to_the_key():
    assert _eligible(team_id=None).scope == "key:10:shop-a"


def test_without_the_header_nothing_is_reused():
    assert _eligible(headers={}) is None
    assert _eligible(headers=None) is None


def test_an_unusable_header_is_an_error_not_a_silent_miss():
    with pytest.raises(InvalidContextId):
        _eligible(headers={"logos-context": "x" * 500})


@pytest.mark.parametrize(
    "path", ["chat/completions", "/chat/completions", "v1/chat/completions", "openai/v1/chat/completions"]
)
def test_chat_completions_is_eligible_under_every_prefix(path):
    assert _eligible(path=path) is not None


@pytest.mark.parametrize("path", ["completions", "embeddings", "responses", "messages", "audio/transcriptions"])
def test_other_endpoints_are_not_eligible(path):
    assert _eligible(path=path) is None


def test_streaming_requests_are_not_eligible():
    assert _eligible(_body(stream=True)) is None
    assert _eligible(_body(stream=False)) is not None


@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
        {"tools": []},
        {"functions": [{"name": "lookup"}]},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"max_tokens": 1},
        {"max_completion_tokens": 1},
        {"stop": ["\n"]},
        {"logprobs": True},
        {"logit_bias": {"50256": -100}},
        {"a_field_added_to_the_api_later": 1},
        {"n": 2},
    ],
)
def test_requests_that_shape_the_answer_are_not_eligible(extra):
    # A stored answer cannot be cut to max_tokens, stop at a stop sequence or
    # follow a format, so these requests take the normal path.
    assert _eligible(_body(**extra)) is None


def test_sampling_and_bookkeeping_fields_keep_a_request_eligible():
    body = _body(model="local-llama", temperature=0.2, top_p=0.9, seed=7, user="u-1", metadata={"shop": "a"})
    assert _eligible({**body, "store": False, "timeout_s": 30}) is not None


def test_a_field_sent_as_null_counts_as_absent():
    assert _eligible(_body(max_tokens=None, stop=None, tools=None)) is not None


def test_a_single_choice_is_eligible():
    assert _eligible(_body(n=1)) is not None


@pytest.mark.parametrize("role", ["system", "developer"])
def test_an_instruction_beside_the_question_makes_a_request_ineligible(role):
    # The match uses the question alone, so an instruction such as a language
    # would be ignored by a stored answer.
    body = _body(
        {"role": role, "content": "Answer only in German."},
        {"role": "user", "content": "Where is my package?"},
    )
    assert _eligible(body) is None


@pytest.mark.parametrize(
    "messages",
    [
        [
            {"role": "user", "content": "I ordered shoes."},
            {"role": "assistant", "content": "Thanks, how can I help?"},
            {"role": "user", "content": "Where is my package?"},
        ],
        [{"role": "user", "content": "Hi"}, {"role": "user", "content": "Where is my package?"}],
        [{"role": "user", "content": "Where is it?"}, {"role": "tool", "content": "{}"}],
        [{"role": "system", "content": "Only a setup message."}],
        ["Where is my package?"],
        [],
    ],
)
def test_anything_but_one_user_question_is_not_eligible(messages):
    assert _eligible({"messages": messages}) is None


def test_a_user_message_with_another_field_is_not_eligible():
    # A speaker name reaches the model on the normal path but not the match.
    body = _body({"role": "user", "content": "Where is my package?", "name": "alice"})
    assert _eligible(body) is None


def test_a_message_field_sent_as_null_counts_as_absent():
    body = _body({"role": "user", "content": "Where is my package?", "name": None})
    assert _eligible(body).question == "Where is my package?"


def test_a_question_with_images_or_parts_is_not_eligible():
    body = _body({"role": "user", "content": [{"type": "text", "text": "Where is my package?"}]})
    assert _eligible(body) is None


@pytest.mark.parametrize("content", ["", "   ", "{{Order Number}}", "{{Order Number}} {{Name}}"])
def test_an_empty_question_after_normalization_is_not_eligible(content):
    # Every empty question embeds identically and would match any other
    # with similarity 1.0.
    assert _eligible(_body({"role": "user", "content": content})) is None


def test_a_body_without_messages_is_not_eligible():
    assert _eligible({"prompt": "Where is my package?"}) is None


# the in-memory cache


def _entry(entry_id, vector, intent="track_order", source="seed", source_model_id=None):
    v = np.asarray(vector, dtype=np.float32)
    return {
        "id": entry_id,
        "intent": intent,
        "answer_template": f"answer {entry_id}",
        "source": source,
        "source_model_id": source_model_id,
        "embedding": list(v / np.linalg.norm(v)),
    }


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Store:
    """Stored entries per scope; counts how often each scope is read."""

    def __init__(self, scopes):
        self.scopes = scopes
        self.reads = []

    def __call__(self, scope):
        self.reads.append(scope)
        return self.scopes.get(scope, [])


def _cache(store, vector=(1, 0, 0), clock=None, **kwargs):
    encoded = []

    def encode(text):
        encoded.append(text)
        return np.asarray(vector, dtype=np.float32) / np.linalg.norm(vector)

    cache = AnswerCache(store, encode, clock=clock or _Clock(), **kwargs)
    return cache, encoded


REQUEST = ReuseRequest(scope="team:3:shop-a", question="Where is my package {{Order Number}}?")
NEAR = [(1, 0.05, 0), (1, 0.1, 0), (1, 0.15, 0)]


def test_a_close_agreeing_neighbourhood_returns_the_closest_entry():
    store = _Store({"team:3:shop-a": [_entry(7, NEAR[0]), _entry(8, NEAR[1]), _entry(9, NEAR[2])]})
    cache, _ = _cache(store)

    answer, match = cache.find(REQUEST)

    assert answer.entry_id == 7
    assert answer.answer == "answer 7"
    assert answer.intent == "track_order"
    assert answer.similarity == pytest.approx(match.similarity)
    assert match.accepted


def test_the_question_is_embedded_with_prefix_and_without_placeholders():
    store = _Store({"team:3:shop-a": [_entry(i, v) for i, v in enumerate(NEAR)]})
    cache, encoded = _cache(store)

    cache.find(REQUEST)

    assert encoded == ["query: Where is my package ?"]


def test_a_far_question_is_a_miss_that_reports_its_similarity():
    store = _Store({"team:3:shop-a": [_entry(i, (0, 1, 0)) for i in range(3)]})
    cache, _ = _cache(store)

    answer, match = cache.find(REQUEST)

    assert answer is None
    assert not match.accepted
    assert match.similarity == pytest.approx(0.0, abs=1e-6)


def test_an_empty_scope_is_a_miss_without_running_the_model():
    cache, encoded = _cache(_Store({}))

    answer, match = cache.find(REQUEST)

    assert answer is None
    assert match.index is None
    assert encoded == []


def test_a_scope_with_fewer_entries_than_k_is_a_miss_without_running_the_model():
    # Two near-identical questions could never satisfy top-3 agreement.
    cache, encoded = _cache(_Store({"team:3:shop-a": [_entry(7, NEAR[0]), _entry(8, NEAR[1])]}))

    answer, match = cache.find(REQUEST)

    assert answer is None
    assert not match.accepted
    assert encoded == []


def test_a_scope_never_answers_from_another_scope():
    store = _Store(
        {
            "team:3:shop-a": [],
            "team:7:shop-a": [_entry(i, v) for i, v in enumerate(NEAR)],
        }
    )
    cache, _ = _cache(store)

    answer, _ = cache.find(REQUEST)

    assert answer is None
    assert store.reads == ["team:3:shop-a"]


def test_a_scope_is_read_once_while_fresh():
    store = _Store({"team:3:shop-a": [_entry(i, v) for i, v in enumerate(NEAR)]})
    clock = _Clock()
    cache, _ = _cache(store, clock=clock, ttl_seconds=30)

    cache.find(REQUEST)
    clock.now = 29.9
    cache.find(REQUEST)

    assert store.reads == ["team:3:shop-a"]


def test_a_stale_scope_is_read_again_and_sees_new_answers():
    store = _Store({"team:3:shop-a": []})
    clock = _Clock()
    cache, _ = _cache(store, clock=clock, ttl_seconds=30)

    assert cache.find(REQUEST)[0] is None
    store.scopes["team:3:shop-a"] = [_entry(i, v) for i, v in enumerate(NEAR)]
    clock.now = 30.0

    assert cache.find(REQUEST)[0] is not None
    assert store.reads == ["team:3:shop-a", "team:3:shop-a"]


def test_invalidate_forces_a_new_read():
    store = _Store({"team:3:shop-a": []})
    cache, _ = _cache(store)

    cache.find(REQUEST)
    cache.invalidate("team:3:shop-a")
    cache.find(REQUEST)
    cache.invalidate()
    cache.find(REQUEST)

    assert len(store.reads) == 3


def _ask(cache, scope):
    return cache.find(ReuseRequest(scope=scope, question="Where is my package?"))


def test_distinct_context_names_never_hold_more_than_the_bound():
    # Clients choose context names, so memory must not grow with them.
    store = _Store({})
    cache, _ = _cache(store, max_scopes=3)

    for i in range(100):
        _ask(cache, f"team:3:name-{i}")

    assert list(cache._scopes) == ["team:3:name-97", "team:3:name-98", "team:3:name-99"]


def test_the_least_recently_used_scope_goes_first():
    store = _Store({})
    cache, _ = _cache(store, max_scopes=2)

    _ask(cache, "a")
    _ask(cache, "b")
    _ask(cache, "a")  # a is now the most recently used
    _ask(cache, "c")  # so b goes
    _ask(cache, "a")
    _ask(cache, "b")

    assert store.reads == ["a", "b", "c", "b"]


def test_inactive_scopes_are_dropped_once_their_ttl_has_passed():
    store = _Store({})
    clock = _Clock()
    cache, _ = _cache(store, clock=clock, ttl_seconds=30)

    _ask(cache, "a")
    clock.now = 10.0
    _ask(cache, "b")
    clock.now = 35.0
    _ask(cache, "c")

    assert list(cache._scopes) == ["b", "c"]


def test_the_bound_must_allow_one_scope():
    with pytest.raises(ValueError, match="max_scopes"):
        _cache(_Store({}), max_scopes=0)


# concurrent lookups


class _GatedStore(_Store):
    """A store whose loads of one scope wait until the test opens the gate."""

    def __init__(self, scopes, gated, error=None):
        super().__init__(scopes)
        self.gated = gated
        self.error = error
        self.gate = threading.Event()

    def __call__(self, scope):
        entries = super().__call__(scope)
        if scope == self.gated:
            assert self.gate.wait(5), "the test never opened the gate"
            if self.error is not None:
                raise self.error
        return entries


@pytest.fixture
def waiting(monkeypatch):
    """Counts lookups waiting for another lookup's load of their scope."""
    count = []

    class _CountedEvent(threading.Event):
        def wait(self, timeout=None):
            count.append(1)
            return super().wait(timeout)

    class _CountedRefresh(service._Refresh):
        def __init__(self):
            super().__init__()
            self.done = _CountedEvent()

    monkeypatch.setattr(service, "_Refresh", _CountedRefresh)
    return count


def _wait_until(condition):
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.001)


def _in_threads(calls):
    results = [None] * len(calls)

    def run(i, call):
        try:
            results[i] = call()
        except Exception as exc:  # noqa: BLE001 — the test inspects it
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, call)) for i, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    return threads, results


def test_lookups_for_a_scope_being_loaded_share_one_load(waiting):
    store = _GatedStore({"team:3:shop-a": [_entry(i, v) for i, v in enumerate(NEAR)]}, gated="team:3:shop-a")
    cache, _ = _cache(store)

    threads, results = _in_threads([lambda: cache.find(REQUEST)] * 5)
    _wait_until(lambda: len(waiting) == 4)  # one lookup loads, four wait for it
    store.gate.set()
    for thread in threads:
        thread.join(5)

    assert store.reads == ["team:3:shop-a"]
    assert [answer.entry_id for answer, _ in results] == [0] * 5


def test_a_scope_being_loaded_does_not_hold_up_other_scopes(waiting):
    store = _GatedStore(
        {"team:3:shop-a": [], "team:3:shop-b": [_entry(i, v) for i, v in enumerate(NEAR)]}, gated="team:3:shop-a"
    )
    cache, _ = _cache(store)

    threads, _ = _in_threads([lambda: cache.find(REQUEST)])
    _wait_until(lambda: store.reads == ["team:3:shop-a"])
    answer, _ = cache.find(ReuseRequest(scope="team:3:shop-b", question=REQUEST.question))
    store.gate.set()
    threads[0].join(5)

    assert answer.entry_id == 0


def test_a_failed_load_fails_its_waiters_and_the_next_lookup_loads_again(waiting):
    store = _GatedStore({}, gated="team:3:shop-a", error=RuntimeError("database down"))
    cache, _ = _cache(store)

    threads, results = _in_threads([lambda: cache.find(REQUEST)] * 3)
    _wait_until(lambda: len(waiting) == 2)
    store.gate.set()
    for thread in threads:
        thread.join(5)

    assert [str(result) for result in results] == ["database down"] * 3
    store.error = None
    assert cache.find(REQUEST)[0] is None
    assert store.reads == ["team:3:shop-a", "team:3:shop-a"]


def test_a_model_answer_keeps_its_source_model():
    store = _Store(
        {"team:3:shop-a": [_entry(i, v, source="model", source_model_id=4) for i, v in enumerate(NEAR, start=1)]}
    )
    cache, _ = _cache(store)

    answer, _ = cache.find(REQUEST)

    assert (answer.source, answer.source_model_id) == ("model", 4)


# the cached response

ANSWER = CachedAnswer(
    entry_id=7,
    answer="You can track {{Order Number}} here.",
    intent="track_order",
    similarity=0.91234567,
    source="model",
    source_model_id=4,
)


def test_the_cached_response_has_the_chat_completion_shape():
    payload = chat_completion_payload(ANSWER, created=1_700_000_000)

    assert payload["object"] == "chat.completion"
    assert payload["created"] == 1_700_000_000
    assert payload["id"].startswith("chatcmpl-cache-7-")
    assert payload["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "You can track {{Order Number}} here."},
            "finish_reason": "stop",
        }
    ]


def test_the_cached_response_names_no_model_and_uses_no_tokens():
    payload = chat_completion_payload(ANSWER)

    assert payload["model"] == CACHE_MODEL_NAME
    assert payload["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_the_cached_response_says_which_answer_and_which_model_wrote_it():
    reuse = chat_completion_payload(ANSWER)["logos"]["answer_reuse"]

    assert reuse == {
        "cache_hit": True,
        "entry_id": 7,
        "similarity": 0.9123,
        "intent": "track_order",
        "source": "model",
        "source_model_id": 4,
    }


def test_two_cached_responses_get_different_ids():
    first = chat_completion_payload(ANSWER)
    second = chat_completion_payload(ANSWER)
    assert first["id"] != second["id"]


def test_hit_headers_name_the_entry_and_the_source_model():
    assert cache_hit_headers(ANSWER) == {
        "X-Logos-Cache": "hit",
        "X-Logos-Cache-Entry": "7",
        "X-Logos-Cache-Similarity": "0.9123",
        "X-Logos-Cache-Source-Model": "4",
    }


def test_cors_exposes_every_cache_header_in_the_orchestrator_and_the_gateway():
    # A browser client reads a response header only if CORS exposes it; the
    # orchestrator and the Java gateway each have their own CORS setup.
    from pathlib import Path

    from fastapi.middleware.cors import CORSMiddleware

    from logos import main

    (cors,) = [m for m in main.app.user_middleware if m.cls is CORSMiddleware]
    assert set(cors.kwargs["expose_headers"]) == set(service.CACHE_RESPONSE_HEADERS)

    gateway = Path(__file__).resolve().parents[4] / (
        "logos-webservice/src/main/java/de/tum/cit/aet/logos/logoswebservice/auth/SecurityConfig.java"
    )
    source = gateway.read_text(encoding="utf-8")
    for header in service.CACHE_RESPONSE_HEADERS:
        assert f'"{header}"' in source


def test_a_seeded_answer_has_no_source_model_header():
    seeded = CachedAnswer(7, "a", "track_order", 0.9, "seed", None)
    assert "X-Logos-Cache-Source-Model" not in cache_hit_headers(seeded)
