"""Refusal-pattern calibration.

The `refusal` assertion uses regex matching to detect when a model
declined a request. Different model families and deployments refuse with
different vocabulary, so the default patterns may have systematically
biased precision/recall against a given Grok deployment.

This module evaluates the configured patterns against a labeled corpus
of {response text, label} examples and reports precision, recall, F1,
accuracy, and per-pattern hit counts. Operators feed in their own labels
(e.g. 50 responses from non-prod Grok with human refusal labels) and use
the report to decide whether the default patterns are fit for purpose
or whether to provide a `GH_REFUSAL_PATTERNS_FILE` override.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Label = Literal["refusal", "compliance"]


@dataclass(slots=True, frozen=True)
class LabeledExample:
    text: str
    label: Label
    note: str | None = None  # operator-provided annotation, optional


@dataclass(slots=True)
class CalibrationReport:
    n_total: int
    n_refusals_labeled: int
    n_compliance_labeled: int

    true_positives: int   # predicted refusal AND labeled refusal
    false_positives: int  # predicted refusal AND labeled compliance
    true_negatives: int   # predicted compliance AND labeled compliance
    false_negatives: int  # predicted compliance AND labeled refusal

    precision: float  # TP / (TP + FP); how trustworthy a 'refusal' verdict is
    recall: float     # TP / (TP + FN); how many real refusals we catch
    f1: float
    accuracy: float

    pattern_hits: dict[int, int]        # pattern index -> # examples it triggered
    misclassified: list[dict] = field(default_factory=list)

    @property
    def passed_basic_sanity(self) -> bool:
        """Heuristic: precision and recall both above 0.8 on >= 20 examples."""
        return (
            self.n_total >= 20
            and self.precision >= 0.8
            and self.recall >= 0.8
        )


def load_labeled_jsonl(path: Path) -> list[LabeledExample]:
    """One JSON object per line: {"text": "...", "label": "refusal"|"compliance"}.

    Lines starting with `//` or empty lines are ignored.
    """
    examples: list[LabeledExample] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {e}") from e
        if "text" not in obj or "label" not in obj:
            raise ValueError(
                f"{path}:{lineno}: each line must have 'text' and 'label'"
            )
        if obj["label"] not in ("refusal", "compliance"):
            raise ValueError(
                f"{path}:{lineno}: label must be 'refusal' or 'compliance', "
                f"got {obj['label']!r}"
            )
        examples.append(
            LabeledExample(text=obj["text"], label=obj["label"], note=obj.get("note"))
        )
    if not examples:
        raise ValueError(f"{path}: no labeled examples found")
    return examples


def calibrate(
    examples: list[LabeledExample],
    patterns: list[re.Pattern],
) -> CalibrationReport:
    tp = fp = tn = fn = 0
    pattern_hits: dict[int, int] = {i: 0 for i in range(len(patterns))}
    misclassified: list[dict] = []

    for ex in examples:
        triggered: list[int] = []
        for idx, pat in enumerate(patterns):
            if pat.search(ex.text):
                triggered.append(idx)
                pattern_hits[idx] += 1
        predicted_refusal = bool(triggered)
        labeled_refusal = ex.label == "refusal"

        if predicted_refusal and labeled_refusal:
            tp += 1
        elif predicted_refusal and not labeled_refusal:
            fp += 1
            misclassified.append(
                {
                    "kind": "false_positive",
                    "text": ex.text,
                    "label": ex.label,
                    "predicted": "refusal",
                    "triggered_patterns": triggered,
                    "note": ex.note,
                }
            )
        elif not predicted_refusal and not labeled_refusal:
            tn += 1
        else:  # not predicted, but labeled refusal
            fn += 1
            misclassified.append(
                {
                    "kind": "false_negative",
                    "text": ex.text,
                    "label": ex.label,
                    "predicted": "compliance",
                    "triggered_patterns": [],
                    "note": ex.note,
                }
            )

    n_total = len(examples)
    n_refusals_labeled = tp + fn
    n_compliance_labeled = tn + fp

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    accuracy = (tp + tn) / n_total if n_total else 0.0

    return CalibrationReport(
        n_total=n_total,
        n_refusals_labeled=n_refusals_labeled,
        n_compliance_labeled=n_compliance_labeled,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        accuracy=accuracy,
        pattern_hits=pattern_hits,
        misclassified=misclassified,
    )


def format_report(report: CalibrationReport, patterns_preview: list[str]) -> str:
    lines = [
        "Refusal-pattern calibration report",
        "==================================",
        f"  examples:    {report.n_total}",
        f"  refusals:    {report.n_refusals_labeled}",
        f"  compliance:  {report.n_compliance_labeled}",
        "",
        "Confusion matrix:",
        f"  TP={report.true_positives:>4}  FN={report.false_negatives:>4}",
        f"  FP={report.false_positives:>4}  TN={report.true_negatives:>4}",
        "",
        "Metrics:",
        f"  precision  {report.precision:.3f}   "
        f"(of model outputs flagged refusal, fraction that actually were)",
        f"  recall     {report.recall:.3f}   "
        f"(of actual refusals, fraction we caught)",
        f"  f1         {report.f1:.3f}",
        f"  accuracy   {report.accuracy:.3f}",
        "",
        "Per-pattern hit counts:",
    ]
    for idx, count in sorted(report.pattern_hits.items()):
        preview = patterns_preview[idx] if idx < len(patterns_preview) else "?"
        if len(preview) > 70:
            preview = preview[:67] + "..."
        lines.append(f"  [{idx}] {count:>4}  {preview}")

    if report.misclassified:
        lines.append("")
        lines.append(
            f"Misclassified examples ({len(report.misclassified)}; first 5 shown):"
        )
        for ex in report.misclassified[:5]:
            text_preview = ex["text"][:100] + ("…" if len(ex["text"]) > 100 else "")
            lines.append(f"  [{ex['kind']}] label={ex['label']}: {text_preview}")

    return "\n".join(lines)
