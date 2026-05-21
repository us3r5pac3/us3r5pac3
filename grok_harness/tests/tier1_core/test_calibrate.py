"""Tier 1 (core) — refusal-pattern calibration.

This is part of the prompt-testing capability: the operator measures
how well the refusal detector matches real model outputs before
trusting it as a safety gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grok_harness.calibrate import (
    LabeledExample,
    calibrate,
    format_report,
    load_labeled_jsonl,
)
from grok_harness.evaluators import compile_refusal_patterns

pytestmark = pytest.mark.core


# ----------------------------------------------------------------------------
# 1. Confusion matrix and metrics on a controlled mini dataset.
# ----------------------------------------------------------------------------


def test_perfect_classifier_reports_precision_recall_one():
    patterns = compile_refusal_patterns((r"\bI cannot\b",))
    examples = [
        LabeledExample(text="I cannot help with that.", label="refusal"),
        LabeledExample(text="I cannot comply.", label="refusal"),
        LabeledExample(text="Sure, here you go.", label="compliance"),
        LabeledExample(text="Yes, the answer is 42.", label="compliance"),
    ]
    report = calibrate(examples, patterns)
    assert report.true_positives == 2
    assert report.false_positives == 0
    assert report.true_negatives == 2
    assert report.false_negatives == 0
    assert report.precision == 1.0
    assert report.recall == 1.0
    assert report.f1 == 1.0
    assert report.accuracy == 1.0
    assert report.misclassified == []


def test_false_positive_lowers_precision_not_recall():
    """Pattern matches a compliance example — precision drops, recall stays 1."""
    patterns = compile_refusal_patterns((r"\bcannot\b",))
    examples = [
        LabeledExample(text="I cannot help.", label="refusal"),
        # "cannot" appears, but this is benign — a false positive.
        LabeledExample(
            text="I cannot find that in the data, but I can summarize what's there.",
            label="compliance",
        ),
    ]
    report = calibrate(examples, patterns)
    assert report.true_positives == 1
    assert report.false_positives == 1
    assert report.precision == 0.5
    assert report.recall == 1.0
    assert len(report.misclassified) == 1
    assert report.misclassified[0]["kind"] == "false_positive"


def test_false_negative_lowers_recall_not_precision():
    """A refusal phrasing that no pattern catches — FN, recall drops."""
    patterns = compile_refusal_patterns((r"\bI cannot\b",))
    examples = [
        LabeledExample(text="I cannot help.", label="refusal"),
        LabeledExample(
            text="Sorry, I'm gonna pass on this one.", label="refusal",
            note="default patterns miss this style"
        ),
        LabeledExample(text="Here's the answer.", label="compliance"),
    ]
    report = calibrate(examples, patterns)
    assert report.true_positives == 1
    assert report.false_negatives == 1
    assert report.true_negatives == 1
    assert report.false_positives == 0
    assert report.precision == 1.0
    assert report.recall == pytest.approx(0.5)
    fn_entries = [m for m in report.misclassified if m["kind"] == "false_negative"]
    assert len(fn_entries) == 1
    assert "pass on this one" in fn_entries[0]["text"]


def test_pattern_hits_count_per_pattern():
    """Verify we can see WHICH pattern caught which refusal."""
    patterns = compile_refusal_patterns(
        (
            r"\bI cannot\b",      # pattern 0
            r"\bagainst policy\b",  # pattern 1
        )
    )
    examples = [
        LabeledExample(text="I cannot help with that.", label="refusal"),
        LabeledExample(text="I cannot — against policy.", label="refusal"),
        LabeledExample(text="That request is against policy.", label="refusal"),
    ]
    report = calibrate(examples, patterns)
    assert report.pattern_hits[0] == 2  # both "I cannot" examples
    assert report.pattern_hits[1] == 2  # both "against policy" examples


# ----------------------------------------------------------------------------
# 2. JSONL loading.
# ----------------------------------------------------------------------------


def test_load_labeled_jsonl_ignores_blanks_and_comments(tmp_path: Path):
    p = tmp_path / "labeled.jsonl"
    p.write_text(
        "// header comment\n"
        "\n"
        '{"text": "I cannot help.", "label": "refusal"}\n'
        "// trailing comment\n"
        '{"text": "Sure.", "label": "compliance"}\n'
    )
    examples = load_labeled_jsonl(p)
    assert len(examples) == 2
    assert examples[0].label == "refusal"
    assert examples[1].label == "compliance"


def test_load_labeled_jsonl_rejects_invalid_label(tmp_path: Path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"text": "x", "label": "maybe"}\n')
    with pytest.raises(ValueError, match="label must be"):
        load_labeled_jsonl(p)


def test_load_labeled_jsonl_rejects_missing_fields(tmp_path: Path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"text": "no label here"}\n')
    with pytest.raises(ValueError, match="'text' and 'label'"):
        load_labeled_jsonl(p)


def test_load_labeled_jsonl_rejects_empty_file(tmp_path: Path):
    p = tmp_path / "empty.jsonl"
    p.write_text("// only comments\n\n")
    with pytest.raises(ValueError, match="no labeled examples"):
        load_labeled_jsonl(p)


def test_load_labeled_jsonl_preserves_optional_note(tmp_path: Path):
    p = tmp_path / "labeled.jsonl"
    p.write_text(
        '{"text": "x", "label": "refusal", "note": "edge case"}\n'
        '{"text": "y", "label": "compliance"}\n'
    )
    examples = load_labeled_jsonl(p)
    assert examples[0].note == "edge case"
    assert examples[1].note is None


# ----------------------------------------------------------------------------
# 3. Bundled example dataset must parse and run through the default detector.
# ----------------------------------------------------------------------------


def test_bundled_example_calibrates_against_default_patterns():
    """The example dataset documents the format; it must load and report."""
    from grok_harness.evaluators import _DEFAULT_REFUSAL_PATTERNS

    path = Path(__file__).resolve().parents[2] / "examples" / "refusal-labeled.jsonl"
    examples = load_labeled_jsonl(path)
    assert len(examples) >= 10
    patterns = compile_refusal_patterns(_DEFAULT_REFUSAL_PATTERNS)
    report = calibrate(examples, patterns)
    # The bundled dataset includes intentionally-hard cases (terse refusals,
    # hedging that contains 'cannot'); we don't assert specific precision /
    # recall — that depends on the chosen patterns and is the operator's
    # signal to iterate. Just confirm metrics fall in [0, 1].
    assert 0.0 <= report.precision <= 1.0
    assert 0.0 <= report.recall <= 1.0
    assert report.n_total == len(examples)


def test_format_report_contains_expected_sections():
    patterns = compile_refusal_patterns((r"\bI cannot\b",))
    examples = [
        LabeledExample(text="I cannot help.", label="refusal"),
        LabeledExample(text="Sure.", label="compliance"),
    ]
    report = calibrate(examples, patterns)
    text = format_report(report, [r"\bI cannot\b"])
    assert "Confusion matrix" in text
    assert "precision" in text
    assert "recall" in text
    assert "Per-pattern hit counts" in text
