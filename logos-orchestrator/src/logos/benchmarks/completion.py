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
    baseline_id = nightly.get("baseline_id")
    baseline = next((run for run in measured if run["id"] == baseline_id), None)
    before = nightly.get("original") or {}
    changes = [
        {"parameter": key, "before": before.get(key), "after": value}
        for key, value in (nightly.get("parameters") or {}).items()
        if value != before.get(key)
    ]
    return {
        "benchmark_id": selected["id"],
        "baseline_id": baseline_id,
        "baseline_throughput": rate(baseline) if baseline else None,
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
    comparison = ""
    if (baseline := payload.get("baseline_throughput")) and (throughput := payload.get("throughput")):
        gain = (throughput / baseline - 1) * 100
        comparison = f" {throughput:.1f} tok/s ({gain:+.1f}% vs. the same-round baseline of {baseline:.1f} tok/s)."
    return f" Benchmark #{benchmark}.{comparison} " + (changes + "." if changes else "No parameter changes.")


def status_description(status):
    """Explain interrupted executions without presenting recovery as benchmark completion."""
    return {
        "interrupted_recovered": "Benchmark interrupted; worker configuration recovered",
        "interrupted_recovery": "Benchmark interrupted; worker recovery in progress",
        "recovery_required": "Benchmark interrupted; worker recovery requires attention",
        "interrupted": "Benchmark interrupted",
        "completed": "Benchmark completed",
    }.get(status, status.replace("_", " ").capitalize())


def notification_message(payload):
    """Describe the night's outcome, including recovery and the measured result."""
    status = payload["status"]
    if status == "adoption_failed":
        settings = [
            f"{label} {payload['parameters'][key]}"
            for key, label in (("max_num_seqs", "Max sequences"), ("max_num_batched_tokens", "Max batched tokens"))
            if key in payload["parameters"]
        ]
        benchmark = payload["benchmark_id"]
        gain = (payload["throughput"] / payload["baseline_throughput"] - 1) * 100
        return "Nightly configuration adoption failed", "\n".join(
            [
                f"{payload['provider_name']} · Benchmark #{benchmark} · {' · '.join(settings)}",
                f"Measured {payload['throughput']:.1f} tok/s ({gain:+.1f}% vs. baseline #{payload['baseline_id']}).",
                f"Could not apply the improved configuration. {payload['reason'].rstrip('.')}.",
                "Check the nightly report for the status of the rollback.",
            ]
        )
    title = "Nightly benchmark interrupted" if status.startswith("interrupted") else "Nightly benchmark finished"
    lines = [
        f"{payload['provider_name']} · {payload['successful_configurations']} successful configurations",
        f"{status_description(status)}.",
    ]
    if reason := payload.get("interruption_reason"):
        lines.append(f"Last interruption: {reason.rstrip('.')}.")
    if resumes := payload.get("recovery_resumes"):
        lines.append(f"Retry rounds scheduled after recovery: {resumes}.")
    if payload.get("recovery_retry_limit_reached"):
        lines.append("The nightly retry limit was reached.")
    if adoption := payload.get("production_adoption"):
        lines.append(f"Worker confirmed configuration from benchmark #{adoption['benchmark_id']} on the running lane.")
    else:
        lines.append("Production settings restored.")
    if measurement := description(payload).strip():
        lines.append(measurement)
    return title, "\n".join(lines)
