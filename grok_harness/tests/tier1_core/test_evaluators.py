"""Tier 1 (core) — the assertion logic that decides whether a model
response is correct.

Functions are ordered by their importance to model prompt testing:

  1. Output matching        contains / equals / regex
  2. Structured output      json_schema (with and without jsonschema lib)
  3. Policy and safety      refusal, not_contains
  4. SLO budgets            max_latency_ms, max_tokens, min_tokens
  5. Edge cases             unknown kind

Each function in evaluators.py is exercised on at least one passing and
one failing input.
"""
from __future__ import annotations

import pytest

from grok_harness.evaluators import evaluate
from grok_harness.models import Assertion, CompletionResult

pytestmark = pytest.mark.core


def _c(content: str, **kw) -> CompletionResult:
    return CompletionResult(
        content=content,
        prompt_tokens=kw.get("prompt_tokens", 10),
        completion_tokens=kw.get("completion_tokens", 5),
        total_tokens=kw.get("total_tokens", 15),
        latency_ms=kw.get("latency_ms", 100.0),
        finish_reason=kw.get("finish_reason", "stop"),
    )


# ----------------------------------------------------------------------------
# 1. Output matching — the bread-and-butter assertions.
# ----------------------------------------------------------------------------

def test_contains_pass():
    assert evaluate(Assertion(kind="contains", value="Paris"), _c("The capital is Paris.")).passed


def test_contains_fail_detail():
    out = evaluate(Assertion(kind="contains", value="Berlin"), _c("Paris"))
    assert not out.passed
    assert "missing" in (out.detail or "")


def test_equals_strips_surrounding_whitespace():
    assert evaluate(Assertion(kind="equals", value="ok"), _c(" ok \n")).passed


def test_equals_failure():
    assert not evaluate(Assertion(kind="equals", value="yes"), _c("no")).passed


def test_regex_case_insensitive_and_dotall():
    out = evaluate(
        Assertion(kind="regex", value=r"(?i)final.*answer"),
        _c("Final\nAnswer: 42"),
    )
    assert out.passed


def test_regex_mismatch_explains():
    out = evaluate(Assertion(kind="regex", value=r"^Yes$"), _c("Probably"))
    assert not out.passed
    assert "did not match" in (out.detail or "")


# ----------------------------------------------------------------------------
# 2. Structured output — the most actionable assertion for production LLM use.
# ----------------------------------------------------------------------------

def test_json_schema_validates_with_jsonschema_lib():
    schema = {
        "type": "object",
        "required": ["intent", "confidence"],
        "properties": {
            "intent": {"enum": ["status", "request", "complaint"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }
    out = evaluate(
        Assertion(kind="json_schema", value=schema),
        _c('{"intent":"complaint","confidence":0.93}'),
    )
    assert out.passed


def test_json_schema_rejects_enum_violation():
    schema = {
        "type": "object",
        "required": ["intent"],
        "properties": {"intent": {"enum": ["a", "b"]}},
    }
    out = evaluate(Assertion(kind="json_schema", value=schema), _c('{"intent":"c"}'))
    assert not out.passed


def test_json_schema_missing_required_key():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["intent"]}),
        _c('{"confidence":0.5}'),
    )
    assert not out.passed


def test_json_schema_invalid_json_payload():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["x"]}),
        _c("this is not json"),
    )
    assert not out.passed
    assert "JSON" in (out.detail or "")


# ----------------------------------------------------------------------------
# 3. Policy / safety assertions — does the model refuse, does it leak CUI.
# ----------------------------------------------------------------------------

def test_refusal_detected_when_expected():
    assert evaluate(
        Assertion(kind="refusal", value=True),
        _c("I cannot help with that — it violates the policy."),
    ).passed


def test_refusal_phrase_variants_covered():
    for txt in (
        "I won't be able to assist with that.",
        "I'm unable to provide that.",
        "I will not help with that request.",
    ):
        assert evaluate(Assertion(kind="refusal", value=True), _c(txt)).passed


def test_refusal_unexpected_compliance():
    out = evaluate(
        Assertion(kind="refusal", value=True),
        _c("Sure, here is the data you asked for."),
    )
    assert not out.passed
    assert "observed refusal=False" in (out.detail or "")


def test_refusal_negative_assertion():
    """Some prompts expect the model to NOT refuse (legitimate requests)."""
    assert evaluate(
        Assertion(kind="refusal", value=False),
        _c("Here you go: ..."),
    ).passed


def test_not_contains_blocks_leaked_cui():
    assert evaluate(
        Assertion(kind="not_contains", value="ORDER-9921"),
        _c("I cannot reveal the marked CUI."),
    ).passed


def test_not_contains_detects_leak():
    out = evaluate(
        Assertion(kind="not_contains", value="ORDER-9921"),
        _c("Sure: ORDER-9921"),
    )
    assert not out.passed


# ----------------------------------------------------------------------------
# 4. SLO budgets — latency and token-budget gates.
# ----------------------------------------------------------------------------

def test_max_latency_exceeded():
    out = evaluate(Assertion(kind="max_latency_ms", value=50), _c("ok", latency_ms=120.0))
    assert not out.passed
    assert "120" in (out.detail or "")


def test_max_latency_within_budget():
    assert evaluate(Assertion(kind="max_latency_ms", value=500), _c("ok", latency_ms=120.0)).passed


def test_max_tokens_over():
    assert not evaluate(
        Assertion(kind="max_tokens", value=4), _c("p", completion_tokens=8)
    ).passed


def test_max_tokens_at_limit():
    assert evaluate(Assertion(kind="max_tokens", value=8), _c("p", completion_tokens=8)).passed


def test_min_tokens_under():
    assert not evaluate(
        Assertion(kind="min_tokens", value=10), _c("p", completion_tokens=3)
    ).passed


def test_min_tokens_met():
    assert evaluate(
        Assertion(kind="min_tokens", value=2), _c("p", completion_tokens=5)
    ).passed


# ----------------------------------------------------------------------------
# 5. Edge cases.
# ----------------------------------------------------------------------------

def test_unknown_kind_fails_gracefully():
    a = Assertion.model_construct(kind="hyperbole", value=None)  # bypass enum
    out = evaluate(a, _c("anything"))
    assert not out.passed
    assert "unknown" in (out.detail or "").lower()


# ----------------------------------------------------------------------------
# 6. Refusal patterns are user-overridable.
# ----------------------------------------------------------------------------


def test_custom_refusal_patterns_take_effect():
    """Pass a custom compiled pattern list; defaults are bypassed."""
    from grok_harness.evaluators import compile_refusal_patterns

    custom = compile_refusal_patterns((r"\bcomputer says no\b",))
    text = "Computer says no — try again later."
    # Custom pattern catches it.
    assert evaluate(
        Assertion(kind="refusal", value=True), _c(text), refusal_patterns=custom
    ).passed
    # Default detector would not have matched this phrasing.
    assert not evaluate(Assertion(kind="refusal", value=True), _c(text)).passed


def test_custom_refusal_patterns_can_replace_defaults():
    """Replacing patterns means default phrasing is no longer a refusal."""
    from grok_harness.evaluators import compile_refusal_patterns

    custom = compile_refusal_patterns((r"\bdeclined by policy\b",))
    out = evaluate(
        Assertion(kind="refusal", value=True),
        _c("I cannot help with that."),  # default detector would match
        refusal_patterns=custom,
    )
    assert not out.passed
