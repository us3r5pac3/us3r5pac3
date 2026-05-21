"""Tier 2 (io) — read a YAML prompt suite and validate it before execution.

  1. Happy load          example suite parses, all kinds recognized
  2. Schema rejection    invalid roles, unknown assertion kinds,
                         out-of-range temperature, non-mapping top-level
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grok_harness.loader import load_suite

pytestmark = pytest.mark.io


_KNOWN_ASSERTION_KINDS = {
    "contains",
    "not_contains",
    "regex",
    "equals",
    "json_schema",
    "max_latency_ms",
    "max_tokens",
    "min_tokens",
    "refusal",
}


def _examples_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "examples"


# ----------------------------------------------------------------------------
# 1. Happy load — every shipped example parses, self-identifies, and uses
#    only known assertion kinds.
# ----------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename,expected_name",
    [
        ("functional.yaml", "functional"),
        ("structured.yaml", "structured-output"),
        ("safety.yaml", "safety-policy"),
        ("slo.yaml", "slo"),
        ("full-suite.yaml", "grok-4.3-smoke"),
    ],
)
def test_example_suite_loads_with_known_assertions(filename: str, expected_name: str):
    suite = load_suite(_examples_dir() / filename)
    assert suite.name == expected_name
    assert len(suite.cases) >= 1
    for case in suite.cases:
        for a in case.assertions:
            assert a.kind in _KNOWN_ASSERTION_KINDS


# ----------------------------------------------------------------------------
# 2. Schema rejection — bad suites must fail loudly, not silently.
# ----------------------------------------------------------------------------

def test_top_level_must_be_mapping(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_suite(p)


def test_invalid_role_rejected(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: x\n"
        "cases:\n"
        "  - id: c\n"
        "    messages:\n"
        "      - role: emperor\n"
        "        content: hello\n"
    )
    with pytest.raises(Exception):  # pydantic ValidationError
        load_suite(p)


def test_unknown_assertion_kind_rejected(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: x\n"
        "cases:\n"
        "  - id: c\n"
        "    messages:\n"
        "      - role: user\n"
        "        content: hi\n"
        "    assertions:\n"
        "      - kind: vibes\n"
        "        value: good\n"
    )
    with pytest.raises(Exception):
        load_suite(p)


def test_temperature_range_validated(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: x\n"
        "cases:\n"
        "  - id: c\n"
        "    temperature: 3.0\n"
        "    messages:\n"
        "      - role: user\n"
        "        content: hi\n"
    )
    with pytest.raises(Exception):
        load_suite(p)
