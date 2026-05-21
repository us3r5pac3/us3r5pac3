from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import CaseOutcome


def write_json(outcomes: list[CaseOutcome], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [o.model_dump() for o in outcomes]
    path.write_text(json.dumps(payload, indent=2, default=str))


def write_junit(outcomes: list[CaseOutcome], path: Path, suite_name: str) -> None:
    """Emit JUnit XML so the harness slots into existing CI/ATO test pipelines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = len(outcomes)
    failures = sum(1 for o in outcomes if not o.passed and o.error is None)
    errors = sum(1 for o in outcomes if o.error is not None)
    total_time = sum((o.completion.latency_ms / 1000.0) if o.completion else 0.0 for o in outcomes)

    suites = ET.Element("testsuites")
    suite = ET.SubElement(
        suites,
        "testsuite",
        name=suite_name,
        tests=str(total),
        failures=str(failures),
        errors=str(errors),
        time=f"{total_time:.3f}",
    )
    for o in outcomes:
        tc = ET.SubElement(
            suite,
            "testcase",
            classname=suite_name,
            name=o.case_id,
            time=f"{(o.completion.latency_ms / 1000.0) if o.completion else 0.0:.3f}",
        )
        if o.error:
            err = ET.SubElement(tc, "error", message=o.error[:200])
            err.text = o.error
        elif not o.passed:
            details = "\n".join(
                f"[{a.assertion.kind}] {a.detail}" for a in o.assertions if not a.passed
            )
            fail = ET.SubElement(tc, "failure", message="assertion(s) failed")
            fail.text = details

    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)


def summarize(outcomes: list[CaseOutcome]) -> str:
    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.passed)
    lines = [f"{passed}/{total} passed"]
    for o in outcomes:
        marker = "PASS" if o.passed else "FAIL"
        lat = f"{o.completion.latency_ms:.0f}ms" if o.completion else "no-response"
        lines.append(f"  [{marker}] {o.case_id} ({lat})")
        if o.error:
            lines.append(f"      error: {o.error}")
        for a in o.assertions:
            if not a.passed:
                lines.append(f"      {a.assertion.kind}: {a.detail}")
    return "\n".join(lines)
