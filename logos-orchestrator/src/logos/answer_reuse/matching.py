"""Deciding whether a question may reuse a stored answer, and which intent it has.

Pure functions: no database, no model, no network. A question arrives here
already embedded; the callers load the stored entries and pass them in.

Both decisions apply the same rule to a different set of labelled vectors:
the most similar vector must reach a threshold, and the ``k`` most similar
vectors must all carry the same label. Requiring the top ``k`` to agree keeps
an ambiguous question (whose nearest neighbours belong to different intents)
from being answered with the wrong intent's answer, even when one neighbour
is very close.

All embeddings are expected to be L2-normalized (``normalize_embeddings=True``
in sentence-transformers), so the dot product is the cosine similarity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

# Chosen on held-out questions: of the thresholds tested (0.70 to 0.90 in
# steps of 0.05), with placeholder removal and top-3 agreement, 0.85 was the
# lowest that reused no answer across intents.
DEFAULT_SIMILARITY_THRESHOLD = 0.85
DEFAULT_TOP_K = 3

UNKNOWN_INTENT = "unknown"

# The prefix the classifier's encoder puts in front of every prompt
# (LauraEmbeddingClassifier.encode_text). Stored and incoming questions must be
# encoded with the same prefix, or their similarities are not comparable.
EMBEDDING_PREFIX = "query:"

_PLACEHOLDER = re.compile(r"\{\{.*?\}\}")


def normalize(text: str) -> str:
    """Remove template placeholders and collapse whitespace.

    ``"Cancel order {{Order Number}} please"`` becomes
    ``"Cancel order please"``. A shared placeholder would otherwise make
    unrelated questions from the same template look alike.

    Params:
        text: The question as the client sent it.

    Returns:
        The normalized question; the text the embedding is computed from.
    """
    text = text.strip()
    return " ".join(_PLACEHOLDER.sub(" ", text).split())


def embedding_text(question: str) -> str:
    """Return the exact string to embed for a question.

    Equal to what ``LauraEmbeddingClassifier.encode_text(normalize(question))``
    feeds the model, so a question embedded here and one embedded by the
    classifier land in the same space.

    Params:
        question: The question as the client sent it.

    Returns:
        ``"query: "`` followed by the normalized question.
    """
    return f"{EMBEDDING_PREFIX} {normalize(question)}".strip()


@dataclass(frozen=True)
class Match:
    """The outcome of comparing one question with a set of labelled vectors.

    Attributes:
        accepted: Whether the threshold and the agreement rule both held.
        index: Position of the most similar vector, or None when there was
            nothing to compare with.
        similarity: Cosine similarity to the most similar vector (0.0 when
            there was nothing to compare with). Set for rejected questions
            too.
        label: The label of the most similar vector, or None.
    """

    accepted: bool
    index: Optional[int]
    similarity: float
    label: Optional[str]


def best_match(
    query: Sequence[float],
    vectors: np.ndarray,
    labels: Sequence[str],
    threshold: float,
    k: int,
) -> Match:
    """Compare a question with labelled vectors and apply the reuse rule.

    The rule accepts when
      1. the most similar vector has a similarity of at least ``threshold``, and
      2. the ``k`` most similar vectors all have the same label.

    Fewer than ``k`` vectors cannot satisfy rule 2 and are rejected: with too
    little stored data, agreement cannot be checked.

    Params:
        query: The question's normalized embedding (384 floats).
        vectors: The stored normalized embeddings, one row per entry,
            shape ``(n, 384)``.
        labels: The label of each row of ``vectors``, same length.
        threshold: Minimum cosine similarity of the most similar vector.
        k: How many of the most similar vectors must agree.

    Returns:
        The :class:`Match`; ``accepted`` is False when either rule fails.

    Raises:
        ValueError: ``vectors`` and ``labels`` differ in length, or ``k`` < 1.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    if len(vectors) != len(labels):
        raise ValueError(f"{len(vectors)} vectors but {len(labels)} labels")
    if len(vectors) == 0:
        return Match(accepted=False, index=None, similarity=0.0, label=None)

    similarities = np.asarray(vectors, dtype=np.float32) @ np.asarray(query, dtype=np.float32)
    # Stable sort: among exactly equal similarities the older entry (lower
    # index) wins. Entries with identical vectors can still differ in the
    # last float32 digit, because the matrix product may round rows
    # differently; which of them is picked is then arbitrary, but always one
    # of equally close candidates.
    order = np.argsort(-similarities, kind="stable")
    best = int(order[0])
    best_similarity = float(similarities[best])
    best_label = labels[best]

    accepted = (
        len(vectors) >= k and best_similarity >= threshold and all(labels[int(i)] == best_label for i in order[:k])
    )
    return Match(accepted=accepted, index=best, similarity=best_similarity, label=best_label)


def decide_reuse(
    query: Sequence[float],
    entry_vectors: np.ndarray,
    entry_intents: Sequence[str],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    k: int = DEFAULT_TOP_K,
) -> Match:
    """Decide whether a question may reuse one of a context's stored answers.

    Params:
        query: The question's normalized embedding.
        entry_vectors: The embeddings of the context's answer_cache entries.
        entry_intents: The intent of each entry, same order.
        threshold: Minimum similarity of the closest stored question.
        k: How many closest stored questions must share one intent.

    Returns:
        A :class:`Match`. When ``accepted`` is True, ``index`` names the entry
        whose answer is reused.
    """
    return best_match(query, entry_vectors, entry_intents, threshold, k)


def assign_intent(
    query: Sequence[float],
    exemplar_vectors: np.ndarray,
    exemplar_intents: Sequence[str],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    k: int = DEFAULT_TOP_K,
) -> str:
    """Assign an intent to a question from labelled example questions.

    The same rule as :func:`decide_reuse`, applied to intent_exemplar rows: a
    question whose closest examples are not close enough, or disagree, gets
    :data:`UNKNOWN_INTENT`.

    Params:
        query: The question's normalized embedding.
        exemplar_vectors: The embeddings of the context's example questions.
        exemplar_intents: The intent of each example, same order.
        threshold: Minimum similarity of the closest example.
        k: How many closest examples must share one intent.

    Returns:
        The intent, or :data:`UNKNOWN_INTENT`.
    """
    match = best_match(query, exemplar_vectors, exemplar_intents, threshold, k)
    return match.label if match.accepted else UNKNOWN_INTENT
