"""Merge pytest JUnit XML artifacts without dropping failed phases."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


def _as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def _as_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def merge_junit_files(paths: list[Path], *, dest: Path, name: str = "pytest phases") -> Path:
    """Combine one or more junit XML files into a single ``testsuites`` document.

    Preserves every ``testcase`` (including failures/errors/skips) from every
    input that exists and parses. Missing inputs are skipped with an empty
    suite note so a failed first phase still yields a mergeable artifact when
    later phases never ran.
    """
    suites: list[ET.Element] = []
    for path in paths:
        if not path.is_file():
            suite = ET.Element(
                "testsuite",
                {
                    "name": path.stem,
                    "tests": "0",
                    "failures": "0",
                    "errors": "1",
                    "skipped": "0",
                    "time": "0",
                },
            )
            case = ET.SubElement(
                suite,
                "testcase",
                {"classname": "harness.stack.junit_merge", "name": f"missing:{path.name}", "time": "0"},
            )
            err = ET.SubElement(case, "error", {"message": f"junit artifact missing: {path}"})
            err.text = str(path)
            suites.append(suite)
            continue
        tree = ET.parse(path)
        root = tree.getroot()
        if root.tag == "testsuites":
            suites.extend(list(root))
        elif root.tag == "testsuite":
            suites.append(root)
        else:
            suite = ET.Element(
                "testsuite",
                {
                    "name": path.stem,
                    "tests": "0",
                    "failures": "0",
                    "errors": "1",
                    "skipped": "0",
                    "time": "0",
                },
            )
            case = ET.SubElement(
                suite,
                "testcase",
                {"classname": "harness.stack.junit_merge", "name": f"badroot:{path.name}", "time": "0"},
            )
            err = ET.SubElement(case, "error", {"message": f"unexpected root tag {root.tag}"})
            err.text = path.read_text(encoding="utf-8")[:500]
            suites.append(suite)

    total_tests = sum(_as_int(s.get("tests")) for s in suites)
    total_failures = sum(_as_int(s.get("failures")) for s in suites)
    total_errors = sum(_as_int(s.get("errors")) for s in suites)
    total_skipped = sum(_as_int(s.get("skipped")) for s in suites)
    total_time = sum(_as_float(s.get("time")) for s in suites)

    out_root = ET.Element(
        "testsuites",
        {
            "name": name,
            "tests": str(total_tests),
            "failures": str(total_failures),
            "errors": str(total_errors),
            "skipped": str(total_skipped),
            "time": f"{total_time:.3f}",
        },
    )
    for suite in suites:
        out_root.append(suite)

    dest.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(out_root).write(dest, encoding="utf-8", xml_declaration=True)
    return dest
