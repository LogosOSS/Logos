"""Recovery must be measured independently from streams interrupted by a replica kill."""

import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import failover_under_load as failover  # noqa: E402
import gateway_client as gw  # noqa: E402
import run_benchmark as benchmark  # noqa: E402
from report import build_markdown  # noqa: E402


def response(status=200, complete=True):
    ok = status == 200 and complete
    return gw.RequestResult(ok, status, 1, 100, None if ok else "failed stream", stream_complete=complete)


class RecoveryClassificationTest(unittest.TestCase):
    def test_interrupted_streams_stay_visible_without_hiding_new_request_failures(self):
        rows = [
            (1, 2, response()),
            (9, 30, response(504)),
            (10, 11, response(502)),
            (15, 16, response()),
        ]
        recovery = failover._recovery(rows, 10, 5)
        self.assertTrue(recovery["passed"])
        self.assertEqual(recovery["phases"]["interrupted"]["connectivity_failures"], 1)
        self.assertEqual(recovery["phases"]["routing_update"]["connectivity_failures"], 1)
        totals = failover._classify([row[2] for row in rows])
        self.assertEqual(totals["connectivity_failures"], 2)
        report = build_markdown(
            {
                "env": {"gateway_url": "http://example.invalid"},
                "failover": dict(
                    totals,
                    recovery=recovery,
                    no_visible_impact=False,
                    killed_container="test",
                    kill_after_s=10,
                    duration_s=30,
                    concurrency=32,
                ),
            }
        )
        self.assertIn("Recovery: PASS", report)
        self.assertIn("connectivity failures=2", report)
        self.assertIn("does not assert seamless stream failover", report)

    def test_late_errors_truncation_and_missing_traffic_fail_recovery(self):
        for after in ([], [(15, 16, response(504))], [(15, 16, response(200, False))]):
            with self.subTest(after=after):
                self.assertFalse(failover._recovery([(1, 2, response()), *after], 10, 5)["passed"])
        self.assertFalse(failover._recovery([(15, 16, response())], 10, 5)["passed"])

    def test_errors_before_the_kill_and_other_http_errors_are_not_excused(self):
        for sent, finished, status in ((1, 2, 500), (9, 11, 401), (11, 12, 429), (16, 17, 404)):
            with self.subTest(sent=sent, status=status):
                rows = [(1, 2, response()), (15, 16, response()), (sent, finished, response(status))]
                self.assertFalse(failover._recovery(rows, 10, 5)["passed"])


class RecoveryExitStatusTest(unittest.TestCase):
    def test_required_recovery_cannot_pass_on_failure_missing_data_or_skip(self):
        for result, skip, expected in (
            ({"recovery": {"passed": True}}, "0", 0),
            ({"recovery": {"passed": False}}, "0", 1),
            ({}, "0", 1),
            (None, "0", 1),
            ({"recovery": {"passed": True}}, "1", 1),
            (RuntimeError("not enough replicas"), "0", 1),
        ):
            with self.subTest(result=result, skip=skip), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                stack.enter_context(
                    patch.dict(
                        os.environ,
                        {
                            "LOGOS_BENCH_GW_OUTPUT": tmp,
                            "LOGOS_BENCH_GW_SKIP_BUDGET": "1",
                            "LOGOS_BENCH_GW_SKIP_FAILOVER": skip,
                            "LOGOS_BENCH_GW_FAILOVER_REQUIRE_RECOVERY": "1",
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
                stack.enter_context(patch.object(benchmark.latency_diff, "run", AsyncMock(return_value={})))
                stack.enter_context(patch.object(benchmark.load_generator, "run", AsyncMock(return_value={})))
                runner = (
                    AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
                )
                stack.enter_context(patch.object(benchmark.failover_under_load, "run", runner))
                self.assertEqual(benchmark.run(), expected)
