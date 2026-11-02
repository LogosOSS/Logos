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


class BudgetWarmupTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_warmup_precedes_database_window_and_is_reported_separately(self):
        budget = benchmark.budget_admission
        calls = []

        async def worker(client, url, headers, start, deadline, rows):
            phase = "warmup" if len(calls) < 2 else "measured"
            calls.append(phase)
            rows.append(
                {
                    "sent_at_s": 0,
                    "total_ms": 10000 if phase == "warmup" else 2,
                    "status": 200,
                    "ok": True,
                    "error": None,
                }
            )

        def clock():
            self.assertEqual(calls, ["warmup", "warmup"])
            return "2026-01-01T00:00:00Z"

        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "LOGOS_BENCH_GW_BUDGET_CONCURRENCY": "2",
                        "LOGOS_BENCH_GW_BUDGET_BASELINE": "0",
                        "LOGOS_BENCH_GW_BUDGET_WARMUP_S": "15",
                        "LOGOS_BENCH_GW_BUDGET_LOGOS_P50_MAX_MS": "5",
                        "LOGOS_BENCH_GW_BUDGET_LOGOS_P99_MAX_MS": "50",
                    },
                )
            )
            stack.enter_context(
                patch.object(budget.gw, "post_chat_completion", AsyncMock(return_value=Mock(status_code=200)))
            )
            stack.enter_context(patch.object(budget, "budget_query_snapshot", return_value={}))
            stack.enter_context(patch.object(budget, "_worker", side_effect=worker))
            stack.enter_context(patch.object(budget, "_db_now", side_effect=clock))
            stack.enter_context(
                patch.object(budget, "logos_time", return_value={"n": 2, "logos": {"p50_ms": 2, "p99_ms": 60}})
            )
            result = await budget.run()
        self.assertEqual(result["warmup"], {"duration_s": 15, "requests": 2, "failed": 0})
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["load"]["max_ms"], 2)
        self.assertTrue(result["threshold"]["exceeded"])
        self.assertEqual(calls, ["warmup", "warmup", "measured", "measured"])


if __name__ == "__main__":
    unittest.main()
