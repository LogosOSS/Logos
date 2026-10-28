"""Bounded synthetic workload runner for the GPU-less integrated stack.

Sends real chat-completion requests through the orchestrator OpenAI surface.
Arrival patterns: burst / poisson / sequential / mixed. Stable cross-process
scheduling (no ``hash()`` / PYTHONHASHSEED). Validates OpenAI JSON bodies —
HTTP 200 alone is not success. Simulated timings only; no performance claims.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import httpx
from harness.benchmark_topology import (
    BENCHMARK_MODELS_5,
    BENCHMARK_REQUEST_TEMPLATES,
    BENCHMARK_TRAFFIC_PATTERNS,
    BOUNDED_REQUESTS_PER_PATTERN,
    FULL_SIZE_REPLAY,
    SYNTHETIC_RESULTS_LABEL,
    load_profile_fixture,
)


def stable_pattern_seed(base_seed: int, pattern: str) -> int:
    """Cross-process stable mix of *base_seed* and *pattern* (not PYTHONHASHSEED)."""
    digest = zlib.adler32(pattern.encode("utf-8")) & 0xFFFFFFFF
    return (int(base_seed) ^ digest) & 0x7FFFFFFF


@dataclass
class RequestSpec:
    model: str
    arrival_offset_s: float
    body: dict[str, Any]
    index: int = 0


@dataclass
class RequestResult:
    model: str
    pattern: str
    status_code: int
    ok: bool
    detail: str = ""
    latency_s: float = 0.0
    completion_tokens: int = 0
    text: str = ""


@dataclass
class PatternReport:
    pattern: str
    results: list[RequestResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def fail_count(self) -> int:
        return len(self.results) - self.ok_count

    def models_ok(self) -> set[str]:
        return {r.model for r in self.results if r.ok}


def _body_for(model: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": f"synthetic e2e probe for {model}"},
        ],
        "max_tokens": 16,
        "stream": False,
    }
    tmpl = BENCHMARK_REQUEST_TEMPLATES.get(model) or {}
    body.update(tmpl)
    return body


def validate_chat_completion(payload: Any) -> tuple[bool, str, int, str]:
    """Return (ok, detail, completion_tokens, text)."""
    if not isinstance(payload, dict):
        return False, "response is not a JSON object", 0, ""
    if payload.get("error"):
        return False, f"error field: {payload.get('error')!r}", 0, ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False, "missing choices", 0, ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return False, "missing message", 0, ""
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        # Some engines return empty content with tool/reasoning — still require text.
        return False, f"empty assistant content: {text!r}", 0, ""
    usage = payload.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens") or 0)
    if completion_tokens < 1:
        return False, f"completion_tokens={completion_tokens}", completion_tokens, text
    return True, "", completion_tokens, text


def build_schedule(
    pattern: str,
    *,
    n: int = BOUNDED_REQUESTS_PER_PATTERN,
    models: Sequence[str] = BENCHMARK_MODELS_5,
    seed: int = 42,
    mean_interarrival_s: float = 0.05,
) -> list[RequestSpec]:
    if pattern not in BENCHMARK_TRAFFIC_PATTERNS:
        raise ValueError(f"unknown pattern {pattern!r}; expected one of {BENCHMARK_TRAFFIC_PATTERNS}")
    rng = random.Random(stable_pattern_seed(seed, pattern))
    model_list = list(models)
    if not model_list:
        raise ValueError("models must be non-empty")
    # Round-robin by stable index so every model appears when n >= len(models).
    specs: list[RequestSpec] = []
    t = 0.0
    for i in range(n):
        model = model_list[i % len(model_list)]
        if pattern == "sequential":
            offset = float(i) * mean_interarrival_s
        elif pattern == "burst":
            offset = 0.0 if i < max(1, n // 2) else mean_interarrival_s * (i - n // 2)
        elif pattern == "poisson":
            t += rng.expovariate(1.0 / max(mean_interarrival_s, 1e-6))
            offset = t
        else:  # mixed
            if i < n // 2:
                offset = 0.0
            else:
                offset = mean_interarrival_s * (i - n // 2 + 1)
        specs.append(RequestSpec(model=model, arrival_offset_s=offset, body=_body_for(model), index=i))
    specs.sort(key=lambda s: (s.arrival_offset_s, s.index))
    return specs


def load_trace_csv(path: Path) -> list[RequestSpec]:
    """Load a package-style arrival trace (model_assigned + optional delay_s)."""
    specs: list[RequestSpec] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        t = 0.0
        for i, row in enumerate(reader):
            model = (row.get("model_assigned") or row.get("model") or "").strip()
            if not model:
                raise ValueError(f"trace row {i} missing model_assigned")
            if "arrival_offset_s" in row and row["arrival_offset_s"] not in (None, ""):
                offset = float(row["arrival_offset_s"])
            elif "delay_s" in row and row["delay_s"] not in (None, ""):
                t += float(row["delay_s"])
                offset = t
            else:
                offset = float(i) * 0.05
            specs.append(RequestSpec(model=model, arrival_offset_s=offset, body=_body_for(model), index=i))
    specs.sort(key=lambda s: (s.arrival_offset_s, s.index))
    return specs


async def _one(
    client: httpx.AsyncClient,
    spec: RequestSpec,
    *,
    pattern: str,
    api_key: str,
    start_mono: float,
) -> RequestResult:
    delay = spec.arrival_offset_s - (time.monotonic() - start_mono)
    if delay > 0:
        await asyncio.sleep(delay)
    t0 = time.monotonic()
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=spec.body,
        )
        latency = time.monotonic() - t0
        if response.status_code != 200:
            return RequestResult(
                model=spec.model,
                pattern=pattern,
                status_code=response.status_code,
                ok=False,
                detail=response.text[:500],
                latency_s=latency,
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            return RequestResult(
                model=spec.model,
                pattern=pattern,
                status_code=response.status_code,
                ok=False,
                detail=f"invalid JSON: {exc}",
                latency_s=latency,
            )
        ok, detail, tokens, text = validate_chat_completion(payload)
        return RequestResult(
            model=spec.model,
            pattern=pattern,
            status_code=response.status_code,
            ok=ok,
            detail=detail,
            latency_s=latency,
            completion_tokens=tokens,
            text=text[:200],
        )
    except Exception as exc:  # noqa: BLE001
        return RequestResult(
            model=spec.model,
            pattern=pattern,
            status_code=0,
            ok=False,
            detail=str(exc),
            latency_s=time.monotonic() - t0,
        )


async def run_pattern(
    base_url: str,
    api_key: str,
    pattern: str,
    *,
    n: int = BOUNDED_REQUESTS_PER_PATTERN,
    timeout_s: float = 180.0,
    seed: int = 42,
    schedule: list[RequestSpec] | None = None,
) -> PatternReport:
    specs = schedule if schedule is not None else build_schedule(pattern, n=n, seed=seed)
    report = PatternReport(pattern=pattern)
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s) as client:
        start = time.monotonic()
        tasks = [
            asyncio.create_task(_one(client, spec, pattern=pattern, api_key=api_key, start_mono=start))
            for spec in specs
        ]
        report.results = list(await asyncio.gather(*tasks))
    return report


async def run_all_patterns(
    base_url: str,
    api_key: str,
    *,
    n: int = BOUNDED_REQUESTS_PER_PATTERN,
    patterns: Sequence[str] = BENCHMARK_TRAFFIC_PATTERNS,
    timeout_s: float = 180.0,
    seed: int = 42,
) -> list[PatternReport]:
    out: list[PatternReport] = []
    for pattern in patterns:
        out.append(await run_pattern(base_url, api_key, pattern, n=n, timeout_s=timeout_s, seed=seed))
    return out


def assert_each_model_completed(report: PatternReport, models: Sequence[str] = BENCHMARK_MODELS_5) -> None:
    """Hard fail on any HTTP/parse error and require every model at least once."""
    failures = [r for r in report.results if not r.ok]
    if failures:
        sample = failures[0]
        raise AssertionError(
            f"pattern={report.pattern}: {len(failures)}/{len(report.results)} failed; "
            f"first {sample.model} status={sample.status_code} detail={sample.detail[:300]}"
        )
    ok_models = report.models_ok()
    missing = [m for m in models if m not in ok_models]
    if missing:
        raise AssertionError(
            f"pattern={report.pattern}: missing completed models {missing}; " f"seen={sorted(ok_models)}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic e2e workload runner")
    parser.add_argument("--base-url", default=os.environ.get("E2E_ORCHESTRATOR_URL", "http://localhost:18190"))
    parser.add_argument("--api-key", default=os.environ.get("E2E_DEVELOPER_KEY", "lg-e2e-developer-key"))
    parser.add_argument("--pattern", choices=BENCHMARK_TRAFFIC_PATTERNS, action="append")
    parser.add_argument("--n", type=int, default=BOUNDED_REQUESTS_PER_PATTERN)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--full-replay", action="store_true", help="run FULL_SIZE_REPLAY num_samples")
    parser.add_argument("--trace", type=Path, help="CSV arrival trace (model_assigned[,arrival_offset_s])")
    parser.add_argument("--profile-fixture", type=Path, help="optional observed profiles JSON")
    parser.add_argument("--out", type=Path, help="write JSON report")
    args = parser.parse_args(argv)

    if args.profile_fixture:
        load_profile_fixture(args.profile_fixture)

    patterns = tuple(args.pattern) if args.pattern else BENCHMARK_TRAFFIC_PATTERNS
    n = int(FULL_SIZE_REPLAY["num_samples"]) if args.full_replay else args.n
    timeout_s = float(FULL_SIZE_REPLAY["request_timeout_s"]) if args.full_replay else args.timeout_s

    async def _run() -> list[PatternReport]:
        if args.trace:
            schedule = load_trace_csv(args.trace)
            # Trace is pattern-agnostic; label as mixed for reporting.
            return [await run_pattern(args.base_url, args.api_key, "mixed", schedule=schedule, timeout_s=timeout_s)]
        return await run_all_patterns(
            args.base_url,
            args.api_key,
            n=n,
            patterns=patterns,
            timeout_s=timeout_s,
            seed=args.seed,
        )

    reports = asyncio.run(_run())
    payload = {
        "label": SYNTHETIC_RESULTS_LABEL,
        "scientific_claims": False,
        "full_replay": bool(args.full_replay),
        "n": n,
        "patterns": [
            {
                "pattern": r.pattern,
                "ok": r.ok_count,
                "fail": r.fail_count,
                "models_ok": sorted(r.models_ok()),
                "results": [
                    {
                        "model": x.model,
                        "ok": x.ok,
                        "status_code": x.status_code,
                        "completion_tokens": x.completion_tokens,
                        "detail": x.detail,
                        "latency_s": x.latency_s,
                    }
                    for x in r.results
                ],
            }
            for r in reports
        ],
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    try:
        for report in reports:
            assert_each_model_completed(report)
    except AssertionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
