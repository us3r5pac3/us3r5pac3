from pathlib import Path

from grok_harness.loader import load_suite


def test_load_example(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "examples" / "prompts.yaml"
    suite = load_suite(src)
    assert suite.name == "grok-4.3-smoke"
    assert len(suite.cases) == 3
    ids = {c.id for c in suite.cases}
    assert {"classify-intent-json", "cui-redaction-policy", "terse-answer"} <= ids


def test_assertion_kinds_validate(tmp_path: Path):
    src = Path(__file__).resolve().parents[1] / "examples" / "prompts.yaml"
    suite = load_suite(src)
    for case in suite.cases:
        for a in case.assertions:
            assert a.kind in {
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
