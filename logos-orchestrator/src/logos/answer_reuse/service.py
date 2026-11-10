"""Answering eligible requests from the answer cache.

``reuse_request_from`` decides whether a request takes part in answer reuse
at all; ``AnswerCache`` holds each scope's stored answers in memory and
applies the reuse rule; ``chat_completion_payload`` turns a stored answer
into the response a client expects from a model.

Only a narrow case is eligible: a non-streaming
chat-completions request with a ``logos-context`` header and exactly one
user message in plain text. Everything else follows the normal path.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import numpy as np

from logos.answer_reuse.context import cache_scope, context_id_from_headers
from logos.answer_reuse.matching import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DEFAULT_TOP_K,
    Match,
    decide_reuse,
    embedding_text,
    normalize,
)

# Stored answers are reloaded from the database at most this often per
# scope, so answers seeded while Logos runs are picked up without a restart.
CACHE_TTL_SECONDS = 30.0

# The ``model`` a cached response reports: no model ran for this request.
CACHE_MODEL_NAME = "logos-answer-cache"

# Message roles that set up a conversation without asking anything. Any
# other role besides the single user message (assistant, tool, function)
# means a multi-turn conversation, whose last question depends on what came
# before it.
_SETUP_ROLES = {"system", "developer"}

# Request fields that change what a correct answer looks like. A stored
# plain-text answer is not a valid reply to a request that sets them.
_ANSWER_SHAPING_FIELDS = ("tools", "functions", "tool_choice", "function_call", "response_format")


@dataclass(frozen=True)
class ReuseRequest:
    """A request that may be answered from the cache.

    Attributes:
        scope: The owner-qualified context the answer must come from
            (see :func:`logos.answer_reuse.context.cache_scope`).
        question: The user's question as sent.
    """

    scope: str
    question: str


def _endpoint(path: str) -> str:
    """Return ``path`` without slashes or an API prefix (``v1/``, ``openai/``)."""
    endpoint = path.strip("/")
    for prefix in ("openai/", "v1/"):
        if endpoint.startswith(prefix):
            endpoint = endpoint[len(prefix) :]
    return endpoint


def reuse_request_from(
    body: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    path: str,
    team_id: Optional[int],
    api_key_id: int,
) -> Optional[ReuseRequest]:
    """Decide whether a request takes part in answer reuse.

    Params:
        body: The request body.
        headers: The request headers.
        path: The endpoint path, e.g. ``"chat/completions"``.
        team_id: The team owning the API key, or None.
        api_key_id: The API key.

    Returns:
        The :class:`ReuseRequest`, or None when the request follows the
        normal path: no ``logos-context`` header, another endpoint,
        streaming, more than one choice, tools or a response format,
        anything but exactly one plain-text user message (system and
        developer messages aside), or a question that is empty once
        placeholders are removed.

    Raises:
        InvalidContextId: The ``logos-context`` header is present but unusable.
    """
    context_id = context_id_from_headers(headers)
    if context_id is None:
        return None
    if _endpoint(path) != "chat/completions":
        return None
    if not isinstance(body, dict) or body.get("stream"):
        return None
    if body.get("n") not in (None, 1):
        return None
    if any(body.get(field) for field in _ANSWER_SHAPING_FIELDS):
        return None

    question = _single_question(body.get("messages"))
    if question is None:
        return None
    return ReuseRequest(scope=cache_scope(context_id, team_id, api_key_id), question=question)


def _single_question(messages: Any) -> Optional[str]:
    """Return the one plain-text user question of a conversation, or None.

    None when the conversation has another number of user messages, any
    role besides user, system and developer, a user message that is not
    plain text, or a question that is empty once placeholders are removed.
    """
    if not isinstance(messages, list):
        return None
    roles = [message.get("role") if isinstance(message, dict) else None for message in messages]
    if any(role != "user" and role not in _SETUP_ROLES for role in roles) or roles.count("user") != 1:
        return None
    question = messages[roles.index("user")].get("content")
    if not isinstance(question, str) or not normalize(question):
        return None
    return question


@dataclass(frozen=True)
class CachedAnswer:
    """A stored answer chosen to answer a request.

    Attributes:
        entry_id: The answer_cache row.
        answer: The stored answer, placeholders included.
        intent: The entry's intent.
        similarity: Cosine similarity between the request and the entry's question.
        source: ``"model"`` or ``"seed"``.
        source_model_id: The model that wrote the answer, None for a seeded one.
    """

    entry_id: int
    answer: str
    intent: str
    similarity: float
    source: str
    source_model_id: Optional[int]


@dataclass
class _LoadedScope:
    loaded_at: float
    entries: List[Dict[str, Any]]
    vectors: np.ndarray
    intents: List[str]


class AnswerCache:
    """The stored answers of every scope in use, held in memory.

    Each scope's entries are read once and kept as one float32 matrix, so a
    lookup is a single matrix product instead of a conversion of every
    stored vector. A scope is read again after ``ttl_seconds``.

    Params:
        load_entries: Reads a scope's entries; returns dicts as
            :meth:`DBManager.get_answer_cache_entries` does.
        encode: Embeds one string and returns its normalized vector.
        ttl_seconds: How long a loaded scope is used before it is read again.
        threshold: Minimum similarity of the closest stored question.
        k: How many closest stored questions must share one intent.
        clock: Seconds from a monotonic clock (replaceable in tests).
    """

    def __init__(
        self,
        load_entries: Callable[[str], List[Dict[str, Any]]],
        encode: Callable[[str], Sequence[float]],
        *,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        k: int = DEFAULT_TOP_K,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load_entries = load_entries
        self._encode = encode
        self._ttl_seconds = ttl_seconds
        self._threshold = threshold
        self._k = k
        self._clock = clock
        self._scopes: Dict[str, _LoadedScope] = {}
        self._lock = threading.Lock()

    def find(self, request: ReuseRequest) -> tuple[Optional[CachedAnswer], Match]:
        """Look for a stored answer the request may reuse.

        Blocking (database read on a stale scope, model inference); call it
        off the event loop.

        Params:
            request: The eligible request.

        Returns:
            The chosen answer (None on a miss) and the :class:`Match` that
            decided it.
        """
        scope = self._scope(request.scope)
        query = np.asarray(self._encode(embedding_text(request.question)), dtype=np.float32)
        match = decide_reuse(query, scope.vectors, scope.intents, threshold=self._threshold, k=self._k)
        if not match.accepted:
            return None, match
        entry = scope.entries[match.index]
        answer = CachedAnswer(
            entry_id=int(entry["id"]),
            answer=entry["answer_template"],
            intent=entry["intent"],
            similarity=match.similarity,
            source=entry["source"],
            source_model_id=entry.get("source_model_id"),
        )
        return answer, match

    def invalidate(self, scope: Optional[str] = None) -> None:
        """Forget one loaded scope, or all of them, so the next lookup reads again."""
        with self._lock:
            if scope is None:
                self._scopes.clear()
            else:
                self._scopes.pop(scope, None)

    def _scope(self, name: str) -> _LoadedScope:
        now = self._clock()
        with self._lock:
            loaded = self._scopes.get(name)
        if loaded is not None and now - loaded.loaded_at < self._ttl_seconds:
            return loaded

        entries = self._load_entries(name)
        if entries:
            vectors = np.asarray([entry["embedding"] for entry in entries], dtype=np.float32)
        else:
            vectors = np.zeros((0, 0), dtype=np.float32)
        loaded = _LoadedScope(
            loaded_at=now,
            entries=entries,
            vectors=vectors,
            intents=[entry["intent"] for entry in entries],
        )
        with self._lock:
            self._scopes[name] = loaded
        return loaded


def chat_completion_payload(answer: CachedAnswer, created: Optional[int] = None) -> Dict[str, Any]:
    """Build the chat-completion response for a stored answer.

    The response has the shape a model's response has, so clients need no
    change. ``model`` names the cache, not a model, because no model ran;
    ``usage`` is zero for the same reason. The ``logos.answer_reuse`` object
    says which stored answer was used and which model originally wrote it.

    Params:
        answer: The stored answer.
        created: Unix seconds for ``created``; now when omitted.

    Returns:
        The response body.
    """
    return {
        "id": f"chatcmpl-cache-{answer.entry_id}-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()) if created is None else created,
        "model": CACHE_MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer.answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "logos": {
            "answer_reuse": {
                "cache_hit": True,
                "entry_id": answer.entry_id,
                "similarity": round(answer.similarity, 4),
                "intent": answer.intent,
                "source": answer.source,
                "source_model_id": answer.source_model_id,
            }
        },
    }


def cache_hit_headers(answer: CachedAnswer) -> Dict[str, str]:
    """Response headers marking a cached answer, readable before parsing the body."""
    headers = {
        "X-Logos-Cache": "hit",
        "X-Logos-Cache-Entry": str(answer.entry_id),
        "X-Logos-Cache-Similarity": f"{answer.similarity:.4f}",
    }
    if answer.source_model_id is not None:
        headers["X-Logos-Cache-Source-Model"] = str(answer.source_model_id)
    return headers
