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
from collections import OrderedDict
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

# At most this many scopes are held in memory. Clients choose context names
# freely, so without a bound every new name would stay in memory for good.
MAX_CACHED_SCOPES = 256

# The ``model`` a cached response reports: no model ran for this request.
CACHE_MODEL_NAME = "logos-answer-cache"

# The only request fields a stored answer can honour. The sampling fields only
# change how random a fresh answer would be, and a stored answer is one such
# answer; stream and n are checked separately. Every other field, such as
# max_tokens, stop, tools, response_format, logprobs or logit_bias, may
# constrain what a correct reply is, so a request that sets one is not
# eligible. Listing the safe fields rather than the unsafe ones keeps a field
# added to the API later out of the cache until someone decides otherwise.
_REUSABLE_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "n",
        "temperature",
        "top_p",
        "seed",
        "user",
        "metadata",
        "store",
        "timeout_s",  # Logos's own request timeout
    }
)

# The only fields the single user message may set. Any other field, such as a
# speaker ``name``, reaches the model on the normal path but plays no part in
# the match, so a message that sets one is not eligible.
_REUSABLE_MESSAGE_FIELDS = frozenset({"role", "content"})


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
        streaming, more than one choice, a request field outside
        ``_REUSABLE_FIELDS``, anything but a single plain-text user message
        with only a role and content, or a question that is empty once
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
    if any(value is not None and field not in _REUSABLE_FIELDS for field, value in body.items()):
        return None

    question = _single_question(body.get("messages"))
    if question is None:
        return None
    return ReuseRequest(scope=cache_scope(context_id, team_id, api_key_id), question=question)


def _single_question(messages: Any) -> Optional[str]:
    """Return the question of a conversation that is one user message, or None.

    A stored answer is matched on the question alone. Any other message would
    change what a correct answer is without taking part in the match: a system
    or developer message can set a language or a format ("Answer only in
    German"), and earlier turns change what the last question refers to. So
    None for anything but a single user message, a user message that sets a
    field besides role and content, a user message that is not plain text,
    or a question that is empty once placeholders are removed.
    """
    if not isinstance(messages, list) or len(messages) != 1:
        return None
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    if any(value is not None and field not in _REUSABLE_MESSAGE_FIELDS for field, value in message.items()):
        return None
    question = message.get("content")
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


class _Refresh:
    """A scope load in progress, shared by every lookup that needs it meanwhile."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.loaded: Optional[_LoadedScope] = None
        self.error: Optional[BaseException] = None


class AnswerCache:
    """The stored answers of every scope in use, held in memory.

    Each scope's entries are read once and kept as one float32 matrix, so a
    lookup is a single matrix product instead of a conversion of every
    stored vector. A scope is read again after ``ttl_seconds``.

    Memory is bounded: at most ``max_scopes`` scopes are held, the least
    recently used going first, and a scope past its TTL is dropped whenever a
    scope is loaded, since its next lookup would read it again anyway. Empty
    scopes are held too, so a context without answers costs one database read
    per TTL rather than one per request. Lookups that need a scope while it
    loads share that one load.

    Params:
        load_entries: Reads a scope's entries; returns dicts as
            :meth:`DBManager.get_answer_cache_entries` does.
        encode: Embeds one string and returns its normalized vector.
        ttl_seconds: How long a loaded scope is used before it is read again.
        max_scopes: How many scopes are held in memory at most.
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
        max_scopes: int = MAX_CACHED_SCOPES,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        k: int = DEFAULT_TOP_K,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load_entries = load_entries
        self._encode = encode
        if max_scopes < 1:
            raise ValueError(f"max_scopes must be at least 1, got {max_scopes}")
        self._ttl_seconds = ttl_seconds
        self._max_scopes = max_scopes
        self._threshold = threshold
        self._k = k
        self._clock = clock
        # Least recently used first.
        self._scopes: OrderedDict[str, _LoadedScope] = OrderedDict()
        self._lock = threading.Lock()
        # Scopes being loaded right now. Only loads in progress are held, so
        # this stays as small as the number of concurrent lookups.
        self._refreshing: Dict[str, _Refresh] = {}

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
        if len(scope.entries) < self._k:
            # The rule needs k stored questions to agree, so fewer can never be
            # reused: answer the miss without running the model.
            return None, Match(accepted=False, index=None, similarity=0.0, label=None)
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
        """Return the scope's entries, loading them if they are missing or expired.

        Lookups that need the same scope while it loads wait for that one load
        instead of each reading the database; lookups for other scopes do not
        wait. A failed load fails every lookup that waited for it.
        """
        now = self._clock()
        with self._lock:
            loaded = self._scopes.get(name)
            if loaded is not None and not self._expired(loaded, now):
                self._scopes.move_to_end(name)
                return loaded
            refresh = self._refreshing.get(name)
            loads_it = refresh is None
            if loads_it:
                refresh = _Refresh()
                self._refreshing[name] = refresh

        if not loads_it:
            refresh.done.wait()
            if refresh.error is not None:
                raise refresh.error
            return refresh.loaded

        try:
            loaded = self._load(name, now)
            with self._lock:
                self._scopes[name] = loaded
                self._scopes.move_to_end(name)
                self._evict(now)
            refresh.loaded = loaded
            return loaded
        except BaseException as exc:
            refresh.error = exc
            raise
        finally:
            with self._lock:
                del self._refreshing[name]
            refresh.done.set()

    def _load(self, name: str, now: float) -> _LoadedScope:
        entries = self._load_entries(name)
        if entries:
            vectors = np.asarray([entry["embedding"] for entry in entries], dtype=np.float32)
        else:
            vectors = np.zeros((0, 0), dtype=np.float32)
        return _LoadedScope(
            loaded_at=now,
            entries=entries,
            vectors=vectors,
            intents=[entry["intent"] for entry in entries],
        )

    def _expired(self, loaded: _LoadedScope, now: float) -> bool:
        return now - loaded.loaded_at >= self._ttl_seconds

    def _evict(self, now: float) -> None:
        """Drop expired scopes, then the least recently used beyond the bound. Caller holds the lock."""
        for name in [name for name, loaded in self._scopes.items() if self._expired(loaded, now)]:
            del self._scopes[name]
        while len(self._scopes) > self._max_scopes:
            self._scopes.popitem(last=False)


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
