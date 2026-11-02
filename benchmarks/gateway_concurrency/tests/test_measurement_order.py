"""Keep deliberate saturation outside the admission latency measurement."""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_benchmark as benchmark  # noqa: E402


class MeasurementOrderTest(unittest.TestCase):
    def test_budget_precedes_overload_and_failed_window_is_not_retried(self):
        events = []

        def phase(name, result):
            async def run():
                events.append(name)
                return result

            return run

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "LOGOS_BENCH_GW_OUTPUT": tmp,
                        "LOGOS_BENCH_GW_SKIP_BUDGET": "0",
                        "LOGOS_BENCH_GW_SKIP_FAILOVER": "0",
                        "LOGOS_BENCH_GW_BUDGET_FAIL_ON_THRESHOLD": "1",
                    },
                )
            )
            process = Mock()
            process.poll.side_effect = [None, 0]
            stack.enter_context(patch.object(benchmark.subprocess, "Popen", return_value=process))
            stack.enter_context(patch.object(benchmark, "_check_gateway_reachable"))
            stack.enter_context(patch.object(benchmark, "_wait_http"))
            stack.enter_context(patch.object(benchmark.time, "sleep"))
            stack.enter_context(patch.object(benchmark, "write_reports", return_value={"md": "report.md"}))
            for module, name, result in (
                (benchmark.latency_diff, "latency", {}),
                (benchmark.budget_admission, "budget", {"threshold": {"exceeded": True}}),
                (benchmark.load_generator, "overload", {}),
                (benchmark.failover_under_load, "failover", {}),
            ):
                stack.enter_context(patch.object(module, "run", AsyncMock(side_effect=phase(name, result))))
            self.assertEqual(benchmark.run(), 1)
        self.assertEqual(events, ["latency", "budget", "overload", "failover"])


if __name__ == "__main__":
    unittest.main()
