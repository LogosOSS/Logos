"""Fresh-stack synthetic policy comparison using real HTTP streaming and worker processes."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from harness.benchmark_topology import BENCHMARK_MODELS_5, BENCHMARK_REQUEST_TEMPLATES, BENCHMARK_TRAFFIC_PATTERNS
from harness.clients.admin import AdminClient
from harness.gpusim.performance import DEFAULT_PROFILE, load_timing_profile
from harness.sim_report import git_head_sha
from harness.stack import compose
from harness.stack.bootstrap_benchmark import bootstrap
from harness.workload import stable_pattern_seed

POLICIES = ("demand", "lru", "simple")


def build_schedule(pattern: str, n: int, seed: int, rate: float, profile: dict) -> list[dict]:
    """Construct one reproducible offered workload, independent of policy and time factor."""
    if pattern not in BENCHMARK_TRAFFIC_PATTERNS or n < 5 or not math.isfinite(rate) or rate <= 0:
        raise ValueError("invalid pattern, request count or arrival rate")
    rng = random.Random(stable_pattern_seed(seed, pattern))
    models = list(BENCHMARK_MODELS_5)
    assignments = [models[i % len(models)] for i in range(n)]
    if pattern == "sequential":
        assignments.sort(key=models.index)
    else:
        rng.shuffle(assignments)
    out = []
    at = 0.0
    for index, model in enumerate(assignments):
        if pattern == "burst":
            at = (index // 50) * 50 / rate
        elif pattern == "poisson":
            at += rng.expovariate(rate)
        elif pattern == "sequential":
            at = index / rate
        elif index >= n // 2:
            at += rng.expovariate(rate)
        values = profile["models"][model]
        prompt_tokens = rng.choice(values["prompt_token_samples"])
        output_tokens = rng.choice(values["output_token_samples"])
        body = {
            "model": model,
            "messages": [{"role": "user", "content": " ".join(["synthetic"] * prompt_tokens)}],
            "max_tokens": output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            **BENCHMARK_REQUEST_TEMPLATES.get(model, {}),
        }
        out.append(
            {
                "index": index,
                "model": model,
                "arrival_s": at,
                "body": body,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
            }
        )
    return out


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; missing outcomes never become zero latency."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(q * len(values)) - 1)]


async def run_requests(schedule: list[dict], *, url: str, speed: float, timeout_s: float) -> list[dict]:
    async with httpx.AsyncClient(
        base_url=url,
        timeout=timeout_s,
        limits=httpx.Limits(max_connections=len(schedule), max_keepalive_connections=100),
    ) as client:
        origin = time.monotonic()
        completed = 0

        async def one(spec: dict) -> dict:
            nonlocal completed
            await asyncio.sleep(max(0, spec["arrival_s"] / speed - (time.monotonic() - origin)))
            started = time.monotonic()
            result = {k: spec[k] for k in ("index", "model", "arrival_s", "prompt_tokens", "output_tokens")}
            result.update(
                ok=False,
                status=0,
                ttft_s=None,
                ttlt_s=None,
                arrival_lag_s=max(0, (started - origin) * speed - spec["arrival_s"]),
            )
            terminal = False
            finished = False
            try:
                async with asyncio.timeout(timeout_s):
                    async with client.stream(
                        "POST",
                        "/v1/chat/completions",
                        headers={"Authorization": f"Bearer {compose.DEVELOPER_KEY}"},
                        json=spec["body"],
                    ) as response:
                        result["status"] = response.status_code
                        if response.status_code != 200:
                            result["error"] = (await response.aread()).decode(errors="replace")[:300]
                        else:
                            async for line in response.aiter_lines():
                                if not line.startswith("data:"):
                                    continue
                                payload = line[5:].strip()
                                if payload == "[DONE]":
                                    terminal = True
                                    break
                                chunk = json.loads(payload)
                                if chunk.get("error"):
                                    result["error"] = str(chunk["error"])[:300]
                                    break
                                for choice in chunk.get("choices", []):
                                    text = (choice.get("delta") or {}).get("content")
                                    if text:
                                        now = (time.monotonic() - started) * speed
                                        if result["ttft_s"] is None:
                                            result["ttft_s"] = now
                                        result["ttlt_s"] = now
                                    if choice.get("finish_reason"):
                                        finished = True
                                if chunk.get("usage"):
                                    result["reported_completion_tokens"] = chunk["usage"].get("completion_tokens")
                result["ok"] = terminal and finished and result["ttft_s"] is not None
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"[:300]
            result["time_to_outcome_s"] = (time.monotonic() - started) * speed
            completed += 1
            if completed % 50 == 0 or completed == len(schedule):
                print(f"  outcomes {completed}/{len(schedule)} ({time.monotonic() - origin:.1f}s wall)", flush=True)
            return result

        return list(await asyncio.gather(*(one(spec) for spec in schedule)))


def summarize(results: list[dict], events: list[dict]) -> dict[str, Any]:
    good = [r for r in results if r["ok"]]
    summary = {"submitted": len(results), "completed": len(good), "failures": len(results) - len(good)}
    for metric in ("ttft_s", "ttlt_s", "time_to_outcome_s"):
        samples = results if metric == "time_to_outcome_s" else good
        for name, q in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]:
            summary[f"{metric}_{name}"] = percentile([r[metric] for r in samples if r[metric] is not None], q)
    counts = Counter(e["event"] for e in events)
    summary.update({key: counts[key] for key in ("cold_load", "sleep", "wake", "wake_rejected", "stop")})
    memory = [d for event in events for d in event.get("memory", [])]
    summary["vram_observations"] = len(memory)
    summary["vram_violations"] = sum(d["used_mb"] > d["total_mb"] + 0.01 or d["used_mb"] < 0 for d in memory)
    last: dict[tuple, str] = {}
    switches = 0
    for event in sorted(events, key=lambda e: e["at"]):
        if event["event"] not in {"cold_load", "wake"}:
            continue
        for gpu in event.get("gpu_devices", []):
            key = (event["node"], gpu)
            switches += key in last and last[key] != event["model"]
            last[key] = event["model"]
    summary["lane_switches"] = switches
    summary["max_arrival_lag_s"] = max((r["arrival_lag_s"] for r in results), default=0)
    return summary


def write_outputs(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = payload.get("expected_cases", [])
    actual = {(case["policy"], case["pattern"], case["seed"]) for case in payload["cases"]}
    payload["matrix_complete"] = bool(expected) and actual == {tuple(item) for item in expected}
    payload["verdict"] = (
        "PASS"
        if payload["matrix_complete"]
        and all(
            not c["summary"]["failures"]
            and not c["summary"]["vram_violations"]
            and c["summary"]["vram_observations"] > 0
            for c in payload["cases"]
        )
        else "INCOMPLETE_OR_FAILED"
    )
    path.write_text(json.dumps(payload, indent=2) + "\n")
    rows = [
        {"policy": c["policy"], "pattern": c["pattern"], "seed": c["seed"], **c["summary"]} for c in payload["cases"]
    ]
    if rows:
        with path.with_suffix(".csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    lines = [
        "# Synthetic residency performance",
        "",
        "Real orchestrator and workers; simulated GPU and token service.",
        "No GPU performance or energy claim. Latencies below are wall seconds multiplied by the time factor.",
        f"Time factor: {payload['time_factor']}x. Profile: `{payload['timing_profile']}`.",
        "",
        "| Policy | Pattern | Seed | Completion | TTFT p50/p95 | TTLT p50/p95 | "
        "Cold | Sleep | Wake | 507 | VRAM violations |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def fmt(value):
        return "n/a" if value is None else f"{value:.2f}"

    for case in payload["cases"]:
        s = case["summary"]
        lines.append(
            f"| {case['policy']} | {case['pattern']} | {case['seed']} | {s['completed']}/{s['submitted']} | "
            f"{fmt(s['ttft_s_p50'])}/{fmt(s['ttft_s_p95'])} | {fmt(s['ttlt_s_p50'])}/{fmt(s['ttlt_s_p95'])} | "
            f"{s['cold_load']} | {s['sleep']} | {s['wake']} | {s['wake_rejected']} | {s['vram_violations']} |"
        )
    path.with_suffix(".md").write_text("\n".join(lines) + "\n")


def configure(*, project: str, speed: float, local_images: bool) -> None:
    if not project.startswith("logos-e2e-perf-"):
        raise ValueError("use an isolated project name beginning logos-e2e-perf-")
    os.environ.update(
        E2E_COMPOSE_PROJECT=project,
        E2E_BENCHMARK_TOPOLOGY="1",
        E2E_PERFORMANCE_OVERLAY=str(Path(__file__).with_name("docker-compose.performance.yaml")),
        E2E_PERF_TIME_FACTOR=str(speed),
        E2E_PERF_CYCLE_S=str(10 / speed),
        E2E_PERF_TENURE_S=str(5 / speed),
        E2E_PERF_COLD_TENURE_S=str(15 / speed),
        E2E_PERF_WAKE_BACKOFF_S=str(15 / speed),
        E2E_PERF_LOAD_BACKOFF_S=str(120 / speed),
        E2E_PERF_IDLE_S=str(60 / speed),
    )
    images = "logos-e2e-bench" if local_images else os.environ.get("E2E_PERF_IMAGE_PROJECT", project)
    for env, service in [
        ("DB", "logos-db"),
        ("ORCHESTRATOR", "logos-orchestrator"),
        ("NODE_A", "node-ada-a"),
        ("NODE_B", "node-ada-b"),
    ]:
        os.environ.setdefault(f"E2E_PERF_{env}_IMAGE", f"{images}-{service}")
    compose._ACTIVE_BENCHMARK_TOPOLOGY = True
    compose.NODE_SERVICES = compose.BENCHMARK_NODE_SERVICES


def run(args: argparse.Namespace) -> int:
    profile = load_timing_profile(args.timing_profile)
    configure(project=args.project, speed=args.time_factor, local_images=args.local_images)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((Path(__file__).parents[1] / "fixtures" / "benchmark_profiles_public.json").read_text())
    for models in fixture["nodes"].values():
        for model, values in models.items():
            timing = profile["models"][model]
            values["cold_load_time_s"] = timing["cold_start_s"] / args.time_factor
            values["wake_from_sleep_time_s"] = timing["wake_s"] / args.time_factor
    seeded = args.out.parent / "timing-calibration.json"
    seeded.write_text(json.dumps(fixture))
    os.environ["E2E_PROFILE_FIXTURE"] = str(seeded.resolve())
    payload = {
        "schema_version": 1,
        "synthetic": True,
        "source_sha": git_head_sha(),
        "time_factor": args.time_factor,
        "timing_profile": profile["profile_id"],
        "timing_profile_sha256": hashlib.sha256(args.timing_profile.read_bytes()).hexdigest(),
        "rate_per_simulated_s": args.rate,
        "provenance": profile["provenance"],
        "limitations": (
            "Planner cycle, tenure, idle sleep and failure backoff are scaled. "
            "Worker status polling and HTTP/process overhead remain wall time. "
            "Compare factor sensitivity before interpreting differences."
        ),
        "cases": [],
    }
    payload["source_dirty"] = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=Path(__file__).parents[3])
    )
    payload["source_diff_sha256"] = hashlib.sha256(
        subprocess.check_output(["git", "diff", "HEAD"], cwd=Path(__file__).parents[3])
    ).hexdigest()
    os.environ["E2E_PERF_TIMING_PROFILE_HOST"] = str(args.timing_profile.resolve())
    policies = args.policy or list(POLICIES)
    patterns = args.pattern or list(BENCHMARK_TRAFFIC_PATTERNS)
    seeds = args.seed or [42, 43, 44]
    payload["expected_cases"] = [
        [policy, pattern, seed] for policy in policies for pattern in patterns for seed in seeds
    ]
    try:
        for seed in seeds:
            for pattern in patterns:
                schedule = build_schedule(pattern, args.requests_per_pattern, seed, args.rate, profile)
                digest = hashlib.sha256(json.dumps(schedule, sort_keys=True).encode()).hexdigest()
                for policy in policies:
                    print(
                        f"CASE {policy}/{pattern}/seed={seed} n={len(schedule)} factor={args.time_factor}", flush=True
                    )
                    os.environ["E2E_RESIDENCY_POLICY"] = policy
                    compose.down(benchmark_topology=True)
                    compose._compose(
                        "up", "-d", "--no-build", "--wait", "--wait-timeout", "180", benchmark_topology=True
                    )
                    compose.wait_for_orchestrator(timeout_s=180)
                    bootstrap(fixture=seeded)
                    with AdminClient(compose.orchestrator_url(), compose.ADMIN_KEY) as admin:
                        state = admin.scheduler_state()
                        # The selected policy is exposed inside the capacity-planner state.
                        actual = state.get("residency_policy")
                        if actual != policy:
                            raise RuntimeError(f"policy mismatch: expected {policy}, observed {actual}")
                    started = time.monotonic()
                    results = asyncio.run(
                        run_requests(
                            schedule, url=compose.orchestrator_url(), speed=args.time_factor, timeout_s=args.timeout_s
                        )
                    )
                    events = []
                    for node in compose.BENCHMARK_NODE_SERVICES:
                        result = compose._compose(
                            "exec", "-T", node, "cat", "/app/state/events.jsonl", capture=True, check=False
                        )
                        events.extend({**json.loads(line), "node": node} for line in result.stdout.splitlines() if line)
                    logs = compose._compose("logs", "--no-color", "--tail", "2000", capture=True, check=False)
                    (args.out.parent / f"{policy}-{pattern}-{seed}.log").write_text(logs.stdout + logs.stderr)
                    summary = summarize(results, events)
                    case = {
                        "policy": policy,
                        "pattern": pattern,
                        "seed": seed,
                        "workload_sha256": digest,
                        "wall_duration_s": time.monotonic() - started,
                        "summary": summary,
                        "requests": results,
                        "events": events,
                    }
                    payload["cases"].append(case)
                    write_outputs(args.out, payload)
                    print(json.dumps(summary), flush=True)
    finally:
        write_outputs(args.out, payload)
        compose.down(benchmark_topology=True)
    return int(
        any(
            c["summary"]["failures"] or c["summary"]["vram_violations"] or not c["summary"]["vram_observations"]
            for c in payload["cases"]
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", choices=POLICIES, action="append")
    parser.add_argument("--pattern", choices=BENCHMARK_TRAFFIC_PATTERNS, action="append")
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--requests-per-pattern", type=int, default=250)
    parser.add_argument("--time-factor", type=float, default=10)
    parser.add_argument("--rate", type=float, default=0.3)
    parser.add_argument("--timeout-s", type=float, default=300)
    parser.add_argument("--timing-profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--project", default="logos-e2e-perf-local")
    parser.add_argument("--local-images", action="store_true", help="reuse existing local simulation images")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.time_factor) or args.time_factor <= 0:
        parser.error("time factor must be finite and positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
