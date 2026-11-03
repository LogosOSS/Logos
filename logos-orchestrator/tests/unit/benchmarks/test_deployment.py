from logos.benchmarks.deployment import deployment_snapshot


def snapshot(lanes=None):
    return {
        "last_heartbeat": "2026-11-03T12:00:00+00:00",
        "runtime": {
            "lanes": (
                lanes
                if lanes is not None
                else [
                    {
                        "model": "test-model",
                        "runtime_state": "running",
                        "lane_config": {"vllm_config": {"max_num_seqs": 4, "max_num_batched_tokens": 2048}},
                    }
                ]
            )
        },
    }


def test_current_configuration_requires_online_unambiguous_worker_telemetry():
    live = snapshot()
    result = deployment_snapshot(live, "test-model", online=True, series_key="series")
    assert result["available"] and result["configuration"]["max_num_seqs"] == 4
    assert result["observed_at"] == live["last_heartbeat"]
    for source, online in [
        (None, False),
        (live, False),
        (snapshot([]), True),
        (snapshot(live["runtime"]["lanes"] * 2), True),
    ]:
        unavailable = deployment_snapshot(source, "test-model", online=online)
        assert not unavailable["available"] and unavailable["configuration"] == {}


def test_temporary_benchmark_configuration_is_marked_as_maintenance():
    result = deployment_snapshot(snapshot(), "test-model", online=True, maintenance=True)
    assert result["maintenance"]
