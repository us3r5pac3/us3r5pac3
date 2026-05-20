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


def test_contains_pass():
    out = evaluate(Assertion(kind="contains", value="Paris"), _c("The capital is Paris."))
    assert out.passed


def test_contains_fail():
    out = evaluate(Assertion(kind="contains", value="Berlin"), _c("Paris"))
    assert not out.passed
    assert "missing" in (out.detail or "")


def test_not_contains_redaction():
    out = evaluate(
        Assertion(kind="not_contains", value="ORDER-9921"),
        _c("I cannot reveal the marked CUI."),
    )
    assert out.passed


def test_regex_case_insensitive():
    out = evaluate(Assertion(kind="regex", value=r"(?i)\bparis\b"), _c("paris"))
    assert out.passed


def test_max_latency():
    out = evaluate(Assertion(kind="max_latency_ms", value=50), _c("ok", latency_ms=120.0))
    assert not out.passed
    assert "120" in (out.detail or "")


def test_refusal_detected():
    out = evaluate(
        Assertion(kind="refusal", value=True),
        _c("I cannot help with that — it violates the policy."),
    )
    assert out.passed


def test_refusal_not_present_when_expected():
    out = evaluate(
        Assertion(kind="refusal", value=True),
        _c("Sure, here is the data you asked for."),
    )
    assert not out.passed


def test_json_schema_required_key_missing():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["intent"]}),
        _c('{"confidence": 0.9}'),
    )
    assert not out.passed


def test_json_schema_required_key_present():
    out = evaluate(
        Assertion(kind="json_schema", value={"required": ["intent"]}),
        _c('{"intent": "complaint", "confidence": 0.9}'),
    )
    assert out.passed


def test_max_tokens_bound():
    out = evaluate(
        Assertion(kind="max_tokens", value=4),
        _c("Paris.", completion_tokens=8),
    )
    assert not out.passed
