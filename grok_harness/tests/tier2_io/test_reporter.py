"""Tier 2 (io) — turn outcomes into something a human or CI reads.

Ordered:

  1. Console summary    what an operator sees in their terminal
  2. JUnit XML          what CI ingests; must distinguish <failure> vs <error>
  3. JSON               full structured result for offline analysis
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from grok_harness.models import (
    Assertion,
    AssertionOutcome,
    CaseOutcome,
    CompletionResult,
)
from grok_harness.reporter import summarize, write_json, write_junit

pytestmark = pytest.mark.io


def _completion(latency_ms: float = 50.0) -> CompletionResult:
    return CompletionResult(
        content="ok",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        latency_ms=latency_ms,
        finish_reason="stop",
        request_id="r1",
    )


def _outcomes() -> list[CaseOutcome]:
    return [
        CaseOutcome(
            case_id="pass-1",
            tags=["smoke"],
            passed=True,
            completion=_completion(80.0),
            assertions=[
                AssertionOutcome(
                    assertion=Assertion(kind="contains", value="ok"),
                    passed=True,
                )
            ],
        ),
        CaseOutcome(
            case_id="fail-1",
            tags=["policy"],
            passed=False,
            completion=_completion(120.0),
            assertions=[
                AssertionOutcome(
                    assertion=Assertion(kind="not_contains", value="leak"),
                    passed=False,
                    detail="forbidden substring 'leak' present",
                )
            ],
        ),
        CaseOutcome(
            case_id="error-1",
            tags=[],
            passed=False,
            completion=None,
            assertions=[],
            error="Grok 4.3 returned 503: boom",
        ),
    ]


# ----------------------------------------------------------------------------
# 1. Console summary — what the operator sees first.
# ----------------------------------------------------------------------------

def test_summarize_text(tmp_path: Path):
    text = summarize(_outcomes())
    assert "1/3 passed" in text
    assert "[PASS] pass-1" in text
    assert "[FAIL] fail-1" in text
    assert "[FAIL] error-1" in text
    assert "503" in text


# ----------------------------------------------------------------------------
# 2. JUnit XML — failures from assertions, errors from infrastructure faults.
# ----------------------------------------------------------------------------

def test_write_junit_distinguishes_failures_from_errors(tmp_path: Path):
    path = tmp_path / "junit.xml"
    write_junit(_outcomes(), path, suite_name="grok-4.3-smoke")

    tree = ET.parse(path)
    suite = tree.getroot().find("testsuite")
    assert suite is not None
    assert suite.get("tests") == "3"
    assert suite.get("failures") == "1"
    assert suite.get("errors") == "1"

    cases = {tc.get("name"): tc for tc in suite.findall("testcase")}
    assert set(cases) == {"pass-1", "fail-1", "error-1"}
    assert cases["pass-1"].find("failure") is None
    assert cases["pass-1"].find("error") is None
    fail_el = cases["fail-1"].find("failure")
    assert fail_el is not None
    assert "not_contains" in (fail_el.text or "")
    err_el = cases["error-1"].find("error")
    assert err_el is not None
    assert "503" in (err_el.get("message") or "")


# ----------------------------------------------------------------------------
# 3. JSON — full structured result for offline analysis.
# ----------------------------------------------------------------------------

def test_write_json_roundtrip(tmp_path: Path):
    path = tmp_path / "out.json"
    write_json(_outcomes(), path)
    data = json.loads(path.read_text())
    assert len(data) == 3
    ids = {row["case_id"] for row in data}
    assert ids == {"pass-1", "fail-1", "error-1"}
    fail_row = next(r for r in data if r["case_id"] == "fail-1")
    assert fail_row["completion"]["request_id"] == "r1"
    err_row = next(r for r in data if r["case_id"] == "error-1")
    assert err_row["completion"] is None
    assert "503" in err_row["error"]
