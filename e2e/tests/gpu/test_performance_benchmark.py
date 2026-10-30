"""Timing inputs, deterministic workloads and honest outcome summaries."""

import json
from types import SimpleNamespace

import pytest
from harness.gpusim.performance import DEFAULT_PROFILE, PerformanceTiming, load_timing_profile
from harness.stack.run_performance_benchmark import build_schedule, percentile, summarize


def test_profile_covers_every_model_with_measured_lengths():
    profile = load_timing_profile()
    assert len(profile["models"]) == 5
    for model in profile["models"].values():
        assert model["sample_count"] >= 150
        assert min(model["output_token_samples"]) > 0
        assert model["cold_start_s"] > model["wake_s"]
    assert "Assumed" in profile["provenance"]["concurrency"]


@pytest.mark.parametrize("pattern", ["burst", "poisson", "sequential", "mixed"])
def test_workload_is_policy_independent_and_has_capacity_pressure(pattern):
    profile = load_timing_profile()
    first = build_schedule(pattern, 250, 42, 0.3, profile)
    assert first == build_schedule(pattern, 250, 42, 0.3, profile)
    assert first != build_schedule(pattern, 250, 43, 0.3, profile)
    assert len(first) == 250
    assert len({r["model"] for r in first}) == 5
    assert all(r["body"]["stream"] for r in first)
    assert all(r["output_tokens"] == r["body"]["max_tokens"] for r in first)
    qwen = next(r for r in first if r["model"].startswith("Qwen/"))
    assert qwen["body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_concurrency_and_time_factor_change_only_service_delay(monkeypatch):
    monkeypatch.setenv("LOGOS_SIM_TIMING_PROFILE", str(DEFAULT_PROFILE))
    monkeypatch.setenv("LOGOS_SIM_TIME_FACTOR", "10")
    timing = PerformanceTiming("microsoft/Phi-4-reasoning")
    one = timing.delay(100, "decode")
    timing.running = 5
    assert timing.delay(100, "decode") == pytest.approx(one * 1.6)
    timing.speed = 1
    assert timing.delay(100, "decode") == pytest.approx(one * 16)


def test_failed_requests_do_not_become_success_latency_samples():
    results = [
        dict(ok=True, ttft_s=2, ttlt_s=5, time_to_outcome_s=6, arrival_lag_s=0),
        dict(ok=False, ttft_s=None, ttlt_s=None, time_to_outcome_s=100, arrival_lag_s=1),
    ]
    events = [
        dict(event="cold_load", at=1, model="a", node="n", gpu_devices=[0], memory=[dict(used_mb=10, total_mb=20)]),
        dict(event="wake", at=2, model="b", node="n", gpu_devices=[0], memory=[dict(used_mb=25, total_mb=20)]),
        dict(event="wake_rejected", at=3, model="c", node="n"),
    ]
    summary = summarize(results, events)
    assert summary["completed"] == 1
    assert summary["ttft_s_p99"] == 2
    assert summary["time_to_outcome_s_p99"] == 100
    assert summary["vram_violations"] == 1
    assert summary["wake_rejected"] == 1
    assert summary["lane_switches"] == 1
    assert percentile([], 0.99) is None


def test_profile_rejects_invalid_rates(tmp_path):
    profile = load_timing_profile()
    next(iter(profile["models"].values()))["decode_tokens_s"] = 0
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError):
        load_timing_profile(path)


def test_generation_uses_requested_length_and_reports_usage(monkeypatch):
    monkeypatch.setenv("LOGOS_SIM_TIMING_PROFILE", str(DEFAULT_PROFILE))
    timing = PerformanceTiming("microsoft/Phi-4-reasoning")
    sleeps = []
    monkeypatch.setattr("harness.gpusim.performance.time.sleep", sleeps.append)
    payloads = []
    handler = SimpleNamespace(
        _send=lambda status, payload: payloads.append(payload), _final_payload=lambda *args: {"tokens": len(args[-1])}
    )
    lane = SimpleNamespace(
        model="microsoft/Phi-4-reasoning", requests_total=0, prompt_tokens_total=0, generation_tokens_total=0
    )
    timing.complete(
        handler, lane, "/v1/chat/completions", {"max_tokens": 123, "messages": [{"content": "synthetic prompt"}]}
    )
    assert payloads == [{"tokens": 123}]
    assert lane.generation_tokens_total == 123
    assert lane.prompt_tokens_total == 2
    assert timing.running == 0
    assert len(sleeps) == 2 and all(delay > 0 for delay in sleeps)
