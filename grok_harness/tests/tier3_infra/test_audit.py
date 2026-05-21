"""Tier 3 (infra) — append-only JSONL audit log with redaction.

Compliance/operational concern. Ordered:

  1. Redaction         prompt/response content is hashed by default
  2. Opt-out           redact_prompts=False keeps the full content
  3. Format            ISO UTC timestamp on every record
  4. Append behavior   sequential writes, parent dir auto-created
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from grok_harness.audit import configure

pytestmark = pytest.mark.infra


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().strip().splitlines() if line]


def test_audit_redacts_content_by_default(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = configure(path, redact_prompts=True)
    log.info("case.end", case_id="c1", response="secret answer about ORDER-9921")

    rows = _read(path)
    assert rows[0]["event"] == "case.end"
    assert "response" not in rows[0]
    assert (
        rows[0]["response_sha256"]
        == hashlib.sha256(b"secret answer about ORDER-9921").hexdigest()
    )
    assert rows[0]["response_len"] == len("secret answer about ORDER-9921")


def test_audit_preserves_content_when_redaction_disabled(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = configure(path, redact_prompts=False)
    log.info("case.end", case_id="c1", response="full text")

    rows = _read(path)
    assert rows[0]["response"] == "full text"
    assert "response_sha256" not in rows[0]


def test_audit_emits_iso_utc_timestamp(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = configure(path, redact_prompts=True)
    log.info("suite.start", suite="s")

    ts = _read(path)[0]["timestamp"]
    # structlog ISO formatter emits trailing "Z" for UTC.
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
    assert ts.endswith("Z")


def test_audit_appends_across_calls(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = configure(path, redact_prompts=True)
    log.info("a", n=1)
    log.info("b", n=2)
    log.info("c", n=3)

    rows = _read(path)
    assert [r["event"] for r in rows] == ["a", "b", "c"]


def test_audit_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "nested" / "subdir" / "audit.jsonl"
    assert not path.parent.exists()
    configure(path, redact_prompts=True)
    assert path.parent.exists()


def test_audit_redact_fields_is_configurable(tmp_path: Path):
    """Custom redact set hashes additional fields and skips defaults not listed."""
    path = tmp_path / "audit.jsonl"
    log = configure(
        path,
        redact_prompts=True,
        redact_fields=("cui_payload", "internal_notes"),
    )
    log.info(
        "case.end",
        case_id="c1",
        response="this is the response",     # not in custom set -> kept as-is
        cui_payload="ORDER-9921-ALPHA",      # in custom set -> hashed
        internal_notes="agent's reasoning",  # in custom set -> hashed
    )

    rows = _read(path)
    row = rows[0]
    assert row["response"] == "this is the response"
    assert "cui_payload" not in row
    assert "cui_payload_sha256" in row
    assert "internal_notes_sha256" in row
