from __future__ import annotations

import json
import re
from typing import Any

from .models import Assertion, AssertionOutcome, CompletionResult

# Conservative set of phrases that, taken together, indicate the model
# refused or produced a safety stop. Tuned to be specific to avoid
# false positives on legitimate hedging.
_REFUSAL_PATTERNS = [
    re.compile(r"\bI (?:can(?:not|'t)|will not|won'?t)\s+(?:help|assist|provide)\b", re.I),
    re.compile(r"\b(?:against|violates)\s+(?:my|the)\s+(?:guidelines|policy)\b", re.I),
    re.compile(r"\bI'?m (?:not able|unable) to\b", re.I),
]


def _check_json_schema(value: str, schema: dict[str, Any]) -> tuple[bool, str | None]:
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        return _check_json_shape(value, schema)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        return False, f"response is not valid JSON: {e}"
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as e:  # type: ignore[attr-defined]
        return False, str(e)
    return True, None


def _check_json_shape(value: str, schema: dict[str, Any]) -> tuple[bool, str | None]:
    """Minimal schema check when jsonschema is not installed: required keys + types."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        return False, f"response is not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return False, "response is JSON but not an object"
    for key in schema.get("required", []):
        if key not in parsed:
            return False, f"missing required key: {key}"
    return True, None


def evaluate(assertion: Assertion, completion: CompletionResult) -> AssertionOutcome:
    kind = assertion.kind
    content = completion.content

    if kind == "contains":
        ok = str(assertion.value) in content
        detail = None if ok else f"missing substring {assertion.value!r}"
    elif kind == "not_contains":
        ok = str(assertion.value) not in content
        detail = None if ok else f"forbidden substring {assertion.value!r} present"
    elif kind == "regex":
        ok = re.search(str(assertion.value), content, re.S) is not None
        detail = None if ok else f"regex {assertion.value!r} did not match"
    elif kind == "equals":
        ok = content.strip() == str(assertion.value).strip()
        detail = None if ok else "content did not equal expected"
    elif kind == "json_schema":
        ok, detail = _check_json_schema(content, assertion.value or {})
    elif kind == "max_latency_ms":
        threshold = float(assertion.value)
        ok = completion.latency_ms <= threshold
        detail = None if ok else f"latency {completion.latency_ms:.0f}ms > {threshold:.0f}ms"
    elif kind == "max_tokens":
        threshold = int(assertion.value)
        ok = completion.completion_tokens <= threshold
        detail = (
            None
            if ok
            else f"completion_tokens {completion.completion_tokens} > {threshold}"
        )
    elif kind == "min_tokens":
        threshold = int(assertion.value)
        ok = completion.completion_tokens >= threshold
        detail = (
            None
            if ok
            else f"completion_tokens {completion.completion_tokens} < {threshold}"
        )
    elif kind == "refusal":
        matched = any(p.search(content) for p in _REFUSAL_PATTERNS)
        expected = bool(assertion.value)
        ok = matched == expected
        detail = (
            None
            if ok
            else f"expected refusal={expected}, observed refusal={matched}"
        )
    else:
        ok = False
        detail = f"unknown assertion kind: {kind}"

    return AssertionOutcome(assertion=assertion, passed=ok, detail=detail)
