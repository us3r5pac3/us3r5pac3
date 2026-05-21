"""Tier 1 (core) — scale-up / throughput robustness testing.

Drives the LoadRunner against a mocked Grok endpoint and verifies:

  1. Step execution        each step runs to its configured concurrency
                           and produces aggregate metrics
  2. Percentile math       p50/p95/p99 derived correctly from latency samples
  3. Error handling        Grok 4xx/5xx are counted, not propagated
  4. SLO gates             threshold breaches mark a step (and the run) failed
  5. Profile factories     step_ramp + sustained build the right shape
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
import structlog

from grok_harness.load import (
    LoadProfile,
    LoadRunner,
    LoadStep,
    SloGates,
    _compute,
    _percentile,
)
from grok_harness.models import Message, TestCase, TestSuite

pytestmark = pytest.mark.core


def _ok(content: str = "ok") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture(autouse=True)
def _stub_federation(monkeypatch):
    """Bypass auth — every get_token returns a stub bearer."""
    from grok_harness import auth

    async def _fake(self):
        return auth.BearerToken(value="stub", expires_at=time.time() + 600)

    monkeypatch.setattr(auth.FederatedTokenProvider, "get_token", _fake)


def _suite() -> TestSuite:
    return TestSuite(
        name="load-target",
        cases=[
            TestCase(
                id="c1",
                messages=[Message(role="user", content="ping")],
                max_tokens=16,
            )
        ],
    )


# ----------------------------------------------------------------------------
# 1. Step execution under load
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_drives_requests_at_configured_concurrency(
    settings, grok_completions_url
):
    settings.max_concurrency = 32  # not the gate; LoadRunner sets its own
    peak = 0
    in_flight = 0
    lock = asyncio.Lock()

    async def _handler(request):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return httpx.Response(200, json=_ok())

    async with respx.mock() as router:
        router.post(grok_completions_url).mock(side_effect=_handler)
        profile = LoadProfile(
            steps=(LoadStep(concurrency=4, duration_s=0.2),)
        )
        report = await LoadRunner(settings, structlog.get_logger("t")).run(
            _suite(), profile
        )

    assert peak == 4
    step = report.steps[0]
    assert step.concurrency == 4
    # 4 workers * 0.2s / ~0.01s per req = ~80 requests, but be generous.
    assert step.requests >= 10
    assert step.successes == step.requests
    assert step.errors == 0


@pytest.mark.asyncio
async def test_step_ramp_visits_each_concurrency_level(
    settings, grok_completions_url
):
    """Step ramp 1, 2, 4 produces three StepMetrics in order."""
    async with respx.mock() as router:
        router.post(grok_completions_url).mock(
            return_value=httpx.Response(200, json=_ok())
        )
        profile = LoadProfile.step_ramp(
            max_concurrency=4, step_duration_s=0.1
        )
        report = await LoadRunner(settings, structlog.get_logger("t")).run(
            _suite(), profile
        )

    assert [s.concurrency for s in report.steps] == [1, 2, 4]
    assert report.passed is True


# ----------------------------------------------------------------------------
# 2. Percentile computation
# ----------------------------------------------------------------------------


def test_percentile_handles_empty_and_single_sample():
    assert _percentile([], 0.95) == 0.0
    assert _percentile([42.0], 0.95) == 42.0


def test_percentile_nearest_rank_on_known_sequence():
    # 1..100 sorted. p50 -> 50, p95 -> 95, p99 -> 99.
    data = [float(i) for i in range(1, 101)]
    assert _percentile(data, 0.50) == 50.0
    assert _percentile(data, 0.95) == 95.0
    assert _percentile(data, 0.99) == 99.0


def test_compute_aggregates_match_known_inputs():
    from grok_harness.load import _Outcome

    outcomes = [
        _Outcome(success=True, latency_ms=float(i), status=200) for i in range(1, 11)
    ]
    metrics = _compute(LoadStep(concurrency=2, duration_s=1.0), outcomes, SloGates())
    assert metrics.requests == 10
    assert metrics.successes == 10
    assert metrics.errors == 0
    assert metrics.latency_ms_p50 == 5.0
    assert metrics.latency_ms_p95 == 10.0
    assert metrics.latency_ms_p99 == 10.0
    assert metrics.latency_ms_max == 10.0
    assert metrics.throughput_rps == 10.0


# ----------------------------------------------------------------------------
# 3. Error handling under load
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_errors_are_counted_by_status_not_propagated(
    settings, grok_completions_url
):
    settings.retry_attempts = 0  # don't mask 429s as retries
    counter = {"n": 0}

    async def _handler(request):
        counter["n"] += 1
        # Every third request returns 429, the rest succeed.
        if counter["n"] % 3 == 0:
            return httpx.Response(429, json={"error": "throttled"})
        return httpx.Response(200, json=_ok())

    async with respx.mock() as router:
        router.post(grok_completions_url).mock(side_effect=_handler)
        profile = LoadProfile(
            steps=(LoadStep(concurrency=2, duration_s=0.2),)
        )
        report = await LoadRunner(settings, structlog.get_logger("t")).run(
            _suite(), profile
        )

    step = report.steps[0]
    assert step.requests > 0
    assert step.errors > 0
    assert step.successes + step.errors == step.requests
    assert 429 in step.errors_by_status
    assert step.errors_by_status[429] == step.errors


# ----------------------------------------------------------------------------
# 4. SLO gates
# ----------------------------------------------------------------------------


def test_p95_latency_gate_fails_step():
    from grok_harness.load import _Outcome

    # 15 fast + 5 very slow -> 25% slow tail, p95 (nearest-rank) lands
    # on a slow sample.
    outcomes = [_Outcome(success=True, latency_ms=10.0, status=200) for _ in range(15)]
    outcomes += [_Outcome(success=True, latency_ms=2000.0, status=200) for _ in range(5)]
    metrics = _compute(
        LoadStep(concurrency=4, duration_s=1.0),
        outcomes,
        SloGates(max_p95_latency_ms=500.0),
    )
    assert metrics.latency_ms_p95 == 2000.0
    assert metrics.passed is False
    assert any("p95 latency" in g for g in metrics.failed_gates)


def test_error_rate_gate_fails_step():
    from grok_harness.load import _Outcome

    outcomes = [
        _Outcome(success=i % 2 == 0, latency_ms=10.0, status=200 if i % 2 == 0 else 500)
        for i in range(10)
    ]
    metrics = _compute(
        LoadStep(concurrency=2, duration_s=1.0),
        outcomes,
        SloGates(max_error_rate=0.10),
    )
    assert metrics.passed is False
    assert any("error rate" in g for g in metrics.failed_gates)


def test_min_throughput_gate_fails_step():
    from grok_harness.load import _Outcome

    outcomes = [_Outcome(success=True, latency_ms=10.0, status=200)]
    metrics = _compute(
        LoadStep(concurrency=1, duration_s=10.0),
        outcomes,
        SloGates(min_throughput_rps=5.0),
    )
    # 1 request / 10 seconds = 0.1 rps, way under threshold.
    assert metrics.passed is False
    assert any("throughput" in g for g in metrics.failed_gates)


def test_all_gates_passing_step_is_passing():
    from grok_harness.load import _Outcome

    outcomes = [_Outcome(success=True, latency_ms=50.0, status=200) for _ in range(50)]
    metrics = _compute(
        LoadStep(concurrency=5, duration_s=5.0),
        outcomes,
        SloGates(
            max_p95_latency_ms=100.0, max_error_rate=0.01, min_throughput_rps=5.0
        ),
    )
    assert metrics.passed is True
    assert metrics.failed_gates == []


# ----------------------------------------------------------------------------
# 5. Profile factories
# ----------------------------------------------------------------------------


def test_step_ramp_doubles_until_max():
    profile = LoadProfile.step_ramp(max_concurrency=16, step_duration_s=5.0)
    assert [s.concurrency for s in profile.steps] == [1, 2, 4, 8, 16]
    assert all(s.duration_s == 5.0 for s in profile.steps)


def test_step_ramp_stops_at_or_below_max():
    """Asymmetric max: ramp should stop without overshooting."""
    profile = LoadProfile.step_ramp(max_concurrency=10, step_duration_s=1.0)
    assert [s.concurrency for s in profile.steps] == [1, 2, 4, 8]


def test_sustained_profile_is_single_step():
    profile = LoadProfile.sustained(concurrency=8, duration_s=30.0)
    assert len(profile.steps) == 1
    assert profile.steps[0].concurrency == 8
    assert profile.steps[0].duration_s == 30.0


def test_load_step_validates_inputs():
    with pytest.raises(ValueError):
        LoadStep(concurrency=0, duration_s=1.0)
    with pytest.raises(ValueError):
        LoadStep(concurrency=1, duration_s=0.0)
