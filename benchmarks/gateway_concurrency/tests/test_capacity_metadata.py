"""Independent gateway I/O limits replace executor pool arithmetic; old reports still render."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import load_generator  # noqa: E402
from report import build_markdown  # noqa: E402


class CapacityMetadataTest(unittest.TestCase):
    def test_live_limits_support_replicas_with_different_capacity(self):
        stats = {"replicas": [{"executor_kind": "virtual-per-task", "limit": n} for n in (264, 128)]}
        with patch.object(load_generator.gw, "gateway_relay_stats", return_value=stats):
            ceiling = load_generator._configured_ceiling(2)
        self.assertEqual(ceiling["gateway_max_in_flight_per_replica"], [264, 128])
        self.assertEqual(ceiling["configured_admission_ceiling"], 392)
        self.assertEqual(ceiling["source"], "live gateway stats")
        text = self.render(ceiling)
        self.assertIn("392", text)
        self.assertIn("virtual-per-task", text)
        self.assertNotIn("queue-capacity", text)

    def test_fallback_is_labeled_and_ignores_background_pool_sizes(self):
        with patch.object(load_generator.gw, "gateway_relay_stats", side_effect=RuntimeError("unavailable")):
            with patch.dict(os.environ, {"LOGOS_GATEWAY_MAX_IN_FLIGHT": "64", "LOGOS_GATEWAY_ASYNC_MAX_SIZE": "9999"}):
                ceiling = load_generator._configured_ceiling(2)
        self.assertEqual(ceiling["configured_admission_ceiling"], 128)
        self.assertIn("fallback", self.render(ceiling))

    def test_historic_pool_metadata_remains_readable(self):
        text = self.render(
            {
                "spring_task_execution_pool_max_size": 64,
                "spring_task_execution_pool_queue_capacity": 200,
                "replicas": 2,
                "approx_admission_ceiling_per_replica": 264,
                "approx_admission_ceiling": 528,
            }
        )
        self.assertIn("queue-capacity", text)
        self.assertIn("528", text)

    @staticmethod
    def render(ceiling):
        return build_markdown(
            {
                "env": {"gateway_url": "http://fake.local"},
                "concurrency": {
                    "configured_ceiling": ceiling,
                    "onset_concurrency": None,
                    "fail_threshold": 0.05,
                    "steps": [],
                },
            }
        )


if __name__ == "__main__":
    unittest.main()
