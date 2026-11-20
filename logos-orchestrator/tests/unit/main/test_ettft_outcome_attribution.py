"""Queue migration must not become a calibration sample for another worker."""

from unittest.mock import MagicMock

import logos as main


def test_changed_dispatch_provider_is_excluded_from_accuracy(monkeypatch):
    record = MagicMock()
    skipped = MagicMock()
    monkeypatch.setattr(main.prom, "record_ettft_outcome", record)
    monkeypatch.setattr(main.prom, "ETTFT_REROUTED_OUTCOMES_TOTAL", skipped)
    main._record_ettft_accuracy(
        {
            "ettft_estimate_ms": 5000.0,
            "schedule_start_s": 10.0,
            "provider_id": 2,
            "ettft_provider_id": 1,
            "ettft_tier": "sleeping",
        }
    )
    record.assert_not_called()
    skipped.inc.assert_called_once()


def test_same_provider_records_the_actual_first_token_interval(monkeypatch):
    record = MagicMock()
    monkeypatch.setattr(main.prom, "record_ettft_outcome", record)
    monkeypatch.setattr(main.time, "perf_counter", lambda: 17.0)
    main._record_ettft_accuracy(
        {
            "ettft_estimate_ms": 5000.0,
            "schedule_start_s": 10.0,
            "provider_id": 1,
            "ettft_provider_id": 1,
            "ettft_tier": "sleeping",
        }
    )
    assert record.call_args.kwargs["actual_ttft_s"] == 7.0
    assert record.call_args.kwargs["estimate_s"] == 5.0
