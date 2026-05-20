"""Coverage of every assertion kind in evaluators.py."""
from __future__ import annotations

from grok_harness.evaluators import evaluate
from grok_harness.models import Assertion, CompletionResult


def _c(content: str, **kw) -> CompletionResult:
    return CompletionResult(
        content=content,
        prompt_tokens=kw.get("prompt_tokens", 10),
        completion_tokens=kw.get("completion_tokens", 5),
        total_tokens=kw.get("total_tokens", 15),
        latency_ms=kw.get("latency_ms", 100.0),
        finish_reason=kw.get("finish_reason", "stop"),
    )


# ---- contains / not_contains / regex / equals ----

def test_contains_pass():
    assert evaluate(Assertion(kind="contains", value="Paris"), _c("The capital is Paris.")).passed


def test_contains_fail_detail():
    out = evaluate(Assertion(kind="contains", value="Berlin"), _c("Paris"))
    assert not out.passed
    assert "missing" in (out.detail or "")


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


def test_equals_strips_surrounding_whitespace():
    assert evaluate(Assertion(kind="equals", value="ok"), _c(" ok \n")).passed


def test_equals_failure():
    out = evaluate(Assertion(kind="equals", value="yes"), _c("no"))
    assert not out.passed


# ---- json_schema (strict path uses jsonschema, installed in dev) ----

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
    out = evaluate(
        Assertion(kind="json_schema", value=schema),
        _c('{"intent":"c"}'),
    )
    assert not out.passed


def test_json_schema_invalid_json_payload():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["x"]}),
        _c("this is not json"),
    )
    assert not out.passed
    assert "JSON" in (out.detail or "")


def test_json_schema_missing_required_key():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["intent"]}),
        _c('{"confidence":0.5}'),
    )
    assert not out.passed


# ---- latency / token budgets ----

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
    assert evaluate(
        Assertion(kind="max_tokens", value=8), _c("p", completion_tokens=8)
    ).passed


def test_min_tokens_under():
    out = evaluate(
        Assertion(kind="min_tokens", value=10),
        _c("p", completion_tokens=3),
    )
    assert not out.passed


def test_min_tokens_met():
    assert evaluate(
        Assertion(kind="min_tokens", value=2),
        _c("p", completion_tokens=5),
    ).passed


# ---- refusal detection ----

def test_refusal_detected_when_expected():
    assert evaluate(
        Assertion(kind="refusal", value=True),
        _c("I cannot help with that — it violates the policy."),
    ).passed


def test_refusal_unexpected_compliance():
    out = evaluate(
        Assertion(kind="refusal", value=True),
        _c("Sure, here is the data you asked for."),
    )
    assert not out.passed
    assert "observed refusal=False" in (out.detail or "")


def test_refusal_negative_assertion():
    """Sometimes you want to ensure the model did NOT refuse."""
    assert evaluate(
        Assertion(kind="refusal", value=False),
        _c("Here you go: ..."),
    ).passed


def test_refusal_phrase_variants_covered():
    for txt in (
        "I won't be able to assist with that.",
        "I'm unable to provide that.",
        "I will not help with that request.",
    ):
        assert evaluate(Assertion(kind="refusal", value=True), _c(txt)).passed


# ---- unknown kind ----

def test_unknown_kind_fails_gracefully():
    a = Assertion.model_construct(kind="hyperbole", value=None)  # bypass enum
    out = evaluate(a, _c("anything"))
    assert not out.passed
    assert "unknown" in (out.detail or "").lower()
