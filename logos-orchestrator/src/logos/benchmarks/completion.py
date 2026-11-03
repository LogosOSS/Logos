"""Capture the concrete measurement and parameter changes behind a completion notification."""

from logos.benchmarks.optimization import rate


def result(runs, report):
    """Keep the result from this round, independently of an older optimizer record."""
    successful = {item.get("benchmark_id") for item in report if item.get("status") == "success"}
    measured = [run for run in runs if run["id"] in successful and rate(run) is not None]
    if not measured:
        return {}
    adopted = next((item.get("benchmark_id") for item in report if item.get("status") == "adopted"), None)
    selected = next((run for run in measured if run["id"] == adopted), None)
    selected = selected or max(measured, key=lambda run: (rate(run), -run["id"]))
    configuration = selected["configuration"]
    nightly = configuration.get("nightly", {})
    before = nightly.get("original") or {}
    changes = [
        {"parameter": key, "before": before.get(key), "after": value}
        for key, value in (nightly.get("parameters") or {}).items()
        if value != before.get(key)
    ]
    return {
        "benchmark_id": selected["id"],
        "baseline_id": nightly.get("baseline_id"),
        "workload_id": configuration.get("workload", {}).get("id"),
        "throughput": rate(selected),
        "changes": changes,
    }


def description(payload):
    """Describe only captured changes, without inferring missing settings."""
    benchmark = payload.get("benchmark_id")
    if not benchmark:
        return ""
    changes = "; ".join(
        f"{item['parameter']}: {item['before'] if item['before'] is not None else 'auto'} → {item['after']}"
        for item in payload.get("changes", [])
    )
    return f" Benchmark #{benchmark}. " + (changes + "." if changes else "No parameter changes.")
