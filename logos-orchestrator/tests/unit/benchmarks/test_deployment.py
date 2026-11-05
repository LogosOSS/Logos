import pytest

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


@pytest.mark.parametrize(
    "runtime_state,sleep_state,available",
    [
        ("loaded", "awake", True),
        ("running", "awake", True),
        ("stopped", "awake", False),
        ("error", "awake", False),
        ("starting", "awake", False),
        ("loaded", "sleeping", False),
        (None, "awake", False),
    ],
)
def test_deployment_requires_a_ready_lane_even_when_the_worker_is_online(runtime_state, sleep_state, available):
    live = snapshot()
    live["runtime"]["lanes"][0].update(runtime_state=runtime_state, sleep_state=sleep_state)

    result = deployment_snapshot(live, "test-model", online=True)

    assert result["available"] is available
    assert result["lane_state"] == runtime_state
    assert result["configuration"] == ({"max_num_seqs": 4, "max_num_batched_tokens": 2048} if available else {})
