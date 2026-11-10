"""Which requests take part in answer reuse, the in-memory cache, and the cached response."""

from __future__ import annotations

import numpy as np
import pytest

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
        {"functions": [{"name": "lookup"}]},
        {"tool_choice": "auto"},
        {"response_format": {"type": "json_object"}},
        {"n": 2},
    ],
)
def test_requests_that_shape_the_answer_are_not_eligible(extra):
    assert _eligible(_body(**extra)) is None


def test_a_single_choice_is_eligible():
    assert _eligible(_body(n=1)) is not None


def test_system_and_developer_messages_are_allowed():
    body = _body(
        {"role": "system", "content": "You are the support assistant of Shop A."},
        {"role": "developer", "content": "Be brief."},
        {"role": "user", "content": "Where is my package?"},
    )
    assert _eligible(body).question == "Where is my package?"


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
        [],
    ],
)
def test_anything_but_one_user_question_is_not_eligible(messages):
    assert _eligible({"messages": messages}) is None


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


def test_an_empty_scope_is_a_miss():
    cache, _ = _cache(_Store({}))

    answer, match = cache.find(REQUEST)

    assert answer is None
    assert match.index is None


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
    assert chat_completion_payload(ANSWER)["id"] != chat_completion_payload(ANSWER)["id"]


def test_hit_headers_name_the_entry_and_the_source_model():
    assert cache_hit_headers(ANSWER) == {
        "X-Logos-Cache": "hit",
        "X-Logos-Cache-Entry": "7",
        "X-Logos-Cache-Similarity": "0.9123",
        "X-Logos-Cache-Source-Model": "4",
    }


def test_a_seeded_answer_has_no_source_model_header():
    seeded = CachedAnswer(7, "a", "track_order", 0.9, "seed", None)
    assert "X-Logos-Cache-Source-Model" not in cache_hit_headers(seeded)
