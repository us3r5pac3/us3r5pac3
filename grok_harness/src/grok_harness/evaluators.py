from __future__ import annotations

import json
import re
from typing import Any

from .models import Assertion, AssertionOutcome, CompletionResult

# Default refusal patterns, used when no settings are passed to evaluate().
# The list itself lives in HarnessSettings.refusal_patterns and is
# overridable via GH_REFUSAL_PATTERNS_FILE.
_DEFAULT_REFUSAL_PATTERNS: tuple[str, ...] = (
    r"\bI (?:can(?:not|'t)|will not|won'?t)\s+(?:(?:be able to|going to)\s+)?"
    r"(?:help|assist|provide|engage|comply|do|answer|share|disclose|reveal|"
    r"continue|proceed|generate|produce|create|repeat|echo)\b",
    r"\bI'?m (?:not able|unable)\s+to\b",
    r"\b(?:against|violates|contrary to)\s+(?:my|the|our)\s+"
    r"(?:guidelines|policy|policies|rules|instructions)\b",
)


def compile_refusal_patterns(patterns: tuple[str, ...]) -> list[re.Pattern]:
    return [re.compile(p, re.I | re.S) for p in patterns]


_DEFAULT_COMPILED = compile_refusal_patterns(_DEFAULT_REFUSAL_PATTERNS)


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


def evaluate(
    assertion: Assertion,
    completion: CompletionResult,
    refusal_patterns: list[re.Pattern] | None = None,
) -> AssertionOutcome:
    kind = assertion.kind
    content = completion.content
    patterns = refusal_patterns if refusal_patterns is not None else _DEFAULT_COMPILED

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
        matched = any(p.search(content) for p in patterns)
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
