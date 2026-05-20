"""Tier 1 (core) — drive a suite of prompts and gather outcomes.

The runner is what turns a YAML suite into the pass/fail report. Tests
are ordered:

  1. Suite execution    mixed pass / fail / error tally is correct
  2. Observability      audit log captures suite + case lifecycle events
  3. Operational gate   bounded concurrency under the configured limit
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
import structlog

from grok_harness.runner import SuiteRunner
from grok_harness.models import Assertion, Message, TestCase, TestSuite

pytestmark = pytest.mark.core


def _ok(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture(autouse=True)
def _stub_federation(monkeypatch):
    """Bypass real federation: get_token() returns a stub bearer."""
    import time

    from grok_harness import auth

    async def _fake(self):
        return auth.BearerToken(value="stub", expires_at=time.time() + 600)

    monkeypatch.setattr(auth.FederatedTokenProvider, "get_token", _fake)


# ----------------------------------------------------------------------------
# 1. Suite execution — the heart of the runner.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_passes_and_fails_cases(settings, grok_completions_url):
    suite = TestSuite(
        name="mixed",
        cases=[
            TestCase(
                id="pass-1",
                messages=[Message(role="user", content="x")],
                assertions=[Assertion(kind="contains", value="paris")],
            ),
            TestCase(
                id="fail-assert",
                messages=[Message(role="user", content="y")],
                assertions=[Assertion(kind="contains", value="berlin")],
            ),
            TestCase(
                id="error-503",
                messages=[Message(role="user", content="z")],
            ),
        ],
    )
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(grok_completions_url)
        route.side_effect = [
            httpx.Response(200, json=_ok("paris")),
            httpx.Response(200, json=_ok("london")),
            httpx.Response(503, json={"error": "boom"}),
            httpx.Response(503, json={"error": "boom"}),
            httpx.Response(503, json={"error": "boom"}),
        ]
        log = structlog.get_logger("test")
        outcomes = await SuiteRunner(settings, log).run(suite)

    by_id = {o.case_id: o for o in outcomes}
    assert by_id["pass-1"].passed is True
    assert by_id["fail-assert"].passed is False
    assert by_id["fail-assert"].completion is not None
    assert by_id["fail-assert"].error is None
    assert by_id["error-503"].passed is False
    assert by_id["error-503"].completion is None
    assert by_id["error-503"].error is not None


# ----------------------------------------------------------------------------
# 2. Observability — every case must produce an audit trail.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_writes_audit_log(settings, grok_completions_url, tmp_path: Path):
    from grok_harness.audit import configure

    audit_path = tmp_path / "audit.jsonl"
    settings.audit_log_path = audit_path
    log = configure(audit_path, redact_prompts=True)

    suite = TestSuite(
        name="audit",
        cases=[TestCase(id="c1", messages=[Message(role="user", content="hello")])],
    )
    async with respx.mock() as router:
        router.post(grok_completions_url).mock(
            return_value=httpx.Response(200, json=_ok("hi"))
        )
        await SuiteRunner(settings, log).run(suite)

    lines = audit_path.read_text().strip().splitlines()
    events = [json.loads(line) for line in lines]
    kinds = {e.get("event") for e in events}
    assert {"suite.start", "case.start", "case.end", "suite.end"} <= kinds

    case_end = next(e for e in events if e.get("event") == "case.end")
    # Long-form response should have been redacted to its hash.
    assert "response_sha256" in case_end
    assert "response" not in case_end
    assert case_end["passed"] is True


# ----------------------------------------------------------------------------
# 3. Operational gate — never exceed configured concurrency.
# ----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runner_respects_concurrency(settings, grok_completions_url):
    import asyncio

    settings.max_concurrency = 2
    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def _handler(request):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json=_ok("ok"))

    suite = TestSuite(
        name="parallel",
        cases=[
            TestCase(id=f"c{i}", messages=[Message(role="user", content="x")])
            for i in range(8)
        ],
    )
    async with respx.mock() as router:
        router.post(grok_completions_url).mock(side_effect=_handler)
        await SuiteRunner(settings, structlog.get_logger("t")).run(suite)

    assert peak <= 2
