"""Attach validated short performance measurements to an existing phased report."""

import json
import sys
from pathlib import Path

from report import validate_performance


def main() -> int:
    path = Path(sys.argv[1])
    report = json.loads(path.read_text())
    performance = json.loads(path.with_name("performance.json").read_text())
    reasons = validate_performance(performance, report["residency_policy"], report["source_sha"])
    if reasons:
        raise ValueError("; ".join(reasons))
    report["performance"] = performance
    path.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
