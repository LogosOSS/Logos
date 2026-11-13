"""The reuse rule (pure functions, no database)."""

from __future__ import annotations

import numpy as np
import pytest

from logos.answer_reuse.matching import (
    DEFAULT_SIMILARITY_THRESHOLD,
    best_match,
    decide_reuse,
    embedding_text,
    normalize,
)


def _unit(*values):
    """A normalized vector, so that dot product = cosine similarity."""
    v = np.asarray(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def _at_similarity(query, similarity):
    """A normalized vector whose cosine similarity to ``query`` is ``similarity``."""
    query = np.asarray(query, dtype=np.float32)
    other = np.zeros_like(query)
    other[1 if query[0] != 0 else 0] = 1.0
    other = other - (other @ query) * query
    other = other / np.linalg.norm(other)
    return similarity * query + np.sqrt(1 - similarity**2) * other


QUERY = _unit(1, 0, 0, 0)


# normalize / embedding_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cancel order {{Order Number}}", "cancel order"),
        ("Where is {{Order Number}} from {{Store}}?", "Where is from ?"),
        ("  track   my\norder  ", "track my order"),
        ("{{Order Number}}", ""),
        ("no placeholder here", "no placeholder here"),
    ],
)
def test_placeholders_are_removed_and_whitespace_collapsed(raw, expected):
    assert normalize(raw) == expected


def test_normalize_matches_the_experiment_cleaning():
    # The threshold was tuned with this exact cleaning; any difference in
    # the text changes the embedding and invalidates the threshold.
    import re

    placeholder = re.compile(r"\{\{.*?\}\}")

    def experiment_clean(text):
        text = text.strip()
        return " ".join(placeholder.sub(" ", text).split())

    samples = ["  cance order {{Order Number}} ", "how to {{Delivery City}}track", "plain"]
    assert [normalize(s) for s in samples] == [experiment_clean(s) for s in samples]


def test_the_embedded_text_matches_the_classifier_encoder():
    # LauraEmbeddingClassifier.encode_text builds f"{prefix} {text.strip()}".strip()
    question = "Where is my package {{Order Number}}?"
    classifier_input = f"query: {normalize(question).strip()}".strip()
    assert embedding_text(question) == classifier_input == "query: Where is my package ?"


# the reuse rule


def test_a_close_agreeing_neighbourhood_is_reused():
    vectors = np.stack([_at_similarity(QUERY, s) for s in (0.95, 0.90, 0.88)])
    match = decide_reuse(QUERY, vectors, ["track_order"] * 3)

    assert match.accepted
    assert match.index == 0
    assert match.label == "track_order"
    assert match.similarity == pytest.approx(0.95, abs=1e-5)


def test_a_similarity_equal_to_the_threshold_is_accepted():
    # 0.85 has no exact float32 form, so the threshold is taken from the
    # vector itself: what is tested is that ">=" (not ">") decides.
    vectors = np.stack([_at_similarity(QUERY, DEFAULT_SIMILARITY_THRESHOLD)] * 3)
    match = decide_reuse(QUERY, vectors, ["track_order"] * 3, threshold=float(vectors[0] @ QUERY))
    assert match.accepted


def test_just_below_the_threshold_is_a_miss_that_still_reports_its_similarity():
    vectors = np.stack([_at_similarity(QUERY, s) for s in (0.84, 0.83, 0.82)])
    match = decide_reuse(QUERY, vectors, ["track_order"] * 3)

    assert not match.accepted
    assert match.index == 0
    assert match.similarity == pytest.approx(0.84, abs=1e-5)


def test_disagreeing_top_three_is_a_miss_even_when_the_closest_is_very_close():
    vectors = np.stack([_at_similarity(QUERY, s) for s in (0.97, 0.92, 0.90)])
    match = decide_reuse(QUERY, vectors, ["cancel_order", "track_order", "cancel_order"])

    assert not match.accepted
    assert match.label == "cancel_order"


def test_only_the_top_three_have_to_agree():
    vectors = np.stack([_at_similarity(QUERY, s) for s in (0.95, 0.93, 0.91, 0.60)])
    match = decide_reuse(QUERY, vectors, ["track_order", "track_order", "track_order", "place_order"])
    assert match.accepted


def test_fewer_entries_than_k_cannot_be_reused():
    vectors = np.stack([_at_similarity(QUERY, s) for s in (0.99, 0.98)])
    assert not decide_reuse(QUERY, vectors, ["track_order"] * 2).accepted


def test_an_empty_cache_is_a_miss():
    match = decide_reuse(QUERY, np.zeros((0, 4), dtype=np.float32), [])
    assert match == match.__class__(accepted=False, index=None, similarity=0.0, label=None)


def test_equally_similar_entries_resolve_to_the_oldest():
    same = _at_similarity(QUERY, 0.95)
    vectors = np.stack([_at_similarity(QUERY, 0.5), same, same, same])
    assert decide_reuse(QUERY, vectors, ["x", "a", "a", "a"]).index == 1


def test_mismatched_vectors_and_labels_are_rejected():
    with pytest.raises(ValueError):
        best_match(QUERY, np.stack([QUERY, QUERY]), ["a"], threshold=0.5, k=1)


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        best_match(QUERY, np.stack([QUERY]), ["a"], threshold=0.5, k=0)


def test_the_rule_matches_the_experiment_on_random_data():
    # The expected decision is computed the way the threshold experiment
    # decides (threshold_holdout_v2.py, outside this repository, mode
    # "agreement"), one question at a time. Random data exercises far more
    # neighbourhoods than hand-written cases.
    rng = np.random.default_rng(0)
    labels = np.array(["cancel_order", "track_order", "place_order"])

    for _ in range(300):
        history = rng.normal(size=(30, 16)).astype(np.float32)
        history /= np.linalg.norm(history, axis=1, keepdims=True)
        history_labels = labels[rng.integers(0, 3, size=30)]
        query = (history[rng.integers(0, 30)] + 0.3 * rng.normal(size=16)).astype(np.float32)
        query /= np.linalg.norm(query)
        threshold = float(rng.uniform(0.5, 0.95))

        sims = (query @ history.T)[None, :]
        order = np.argsort(-sims, axis=1)[:, :3]
        best_sim = sims[0, order[0, 0]]
        top_labels = history_labels[order]
        expected = bool(best_sim >= threshold and (top_labels == top_labels[:, [0]]).all())

        assert decide_reuse(query, history, list(history_labels), threshold=threshold).accepted == expected
