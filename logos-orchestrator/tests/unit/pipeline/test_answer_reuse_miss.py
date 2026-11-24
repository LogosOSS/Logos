"""The pipeline passes an answer-cache miss on to the monitoring recorder."""

from __future__ import annotations

from unittest.mock import MagicMock

from logos.pipeline.pipeline import RequestPipeline


def test_a_miss_is_handed_to_the_recorder():
    pipeline = RequestPipeline.__new__(RequestPipeline)
    pipeline._monitoring = MagicMock()

    pipeline.record_answer_reuse_miss("req-1")

    pipeline._monitoring.record_answer_reuse_miss.assert_called_once_with(
        "req-1", similarity=None, nearest_entry_id=None, agreed=None
    )


def test_the_lookup_values_are_passed_on_unchanged():
    pipeline = RequestPipeline.__new__(RequestPipeline)
    pipeline._monitoring = MagicMock()

    pipeline.record_answer_reuse_miss("req-1", similarity=0.84, nearest_entry_id=7, agreed=True)

    pipeline._monitoring.record_answer_reuse_miss.assert_called_once_with(
        "req-1", similarity=0.84, nearest_entry_id=7, agreed=True
    )
