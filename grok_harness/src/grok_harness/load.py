"""Scale-up load testing for a Grok 4.3 deployment.

A separate execution shape from SuiteRunner:

- SuiteRunner runs each case once and judges correctness.
- LoadRunner picks random cases from the suite, replays them at
  bounded concurrency for a fixed duration per step, and reports
  throughput + latency percentiles + error rates.

A "load profile" is a sequence of steps, each at a fixed concurrency
level for a fixed duration. The default profile is a step ramp
(1, 2, 4, 8, 16) so a single run finds the deployment's knee.

Optional SLO gates fail the run if any step breaches p95 latency,
error rate, or minimum throughput thresholds.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Sequence

import httpx
import structlog

from .auth import build_token_provider
from .client import GrokClient, GrokError
from .config import HarnessSettings
from .models import TestCase, TestSuite


@dataclass(slots=True, frozen=True)
class LoadStep:
    concurrency: int
    duration_s: float

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")


@dataclass(slots=True, frozen=True)
class SloGates:
    """Optional per-step thresholds. None means no gate on that metric."""

    max_p95_latency_ms: float | None = None
    max_error_rate: float | None = None  # 0.0 .. 1.0
    min_throughput_rps: float | None = None


@dataclass(slots=True, frozen=True)
class LoadProfile:
    steps: tuple[LoadStep, ...]
    gates: SloGates = SloGates()

    @staticmethod
    def step_ramp(
        max_concurrency: int = 16,
        step_duration_s: float = 10.0,
        gates: SloGates = SloGates(),
    ) -> "LoadProfile":
        """Doubling ramp 1, 2, 4, ..., up to the next power of two >= max."""
        steps: list[LoadStep] = []
        c = 1
        while c <= max_concurrency:
            steps.append(LoadStep(concurrency=c, duration_s=step_duration_s))
            c *= 2
        return LoadProfile(steps=tuple(steps), gates=gates)

    @staticmethod
    def sustained(
        concurrency: int, duration_s: float, gates: SloGates = SloGates()
    ) -> "LoadProfile":
        return LoadProfile(steps=(LoadStep(concurrency=concurrency, duration_s=duration_s),), gates=gates)


@dataclass(slots=True)
class _Outcome:
    success: bool
    latency_ms: float
    status: int  # 0 for non-HTTP errors


@dataclass(slots=True)
class StepMetrics:
    concurrency: int
    duration_s: float
    requests: int
    successes: int
    errors: int
    errors_by_status: dict[int, int]
    latency_ms_p50: float
    latency_ms_p95: float
    latency_ms_p99: float
    latency_ms_max: float
    throughput_rps: float
    failed_gates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failed_gates

    @property
    def error_rate(self) -> float:
        return self.errors / self.requests if self.requests else 0.0


@dataclass(slots=True)
class LoadReport:
    suite_name: str
    auth_mode: str
    steps: list[StepMetrics]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.steps)


def _percentile(sorted_ms: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (R-7), matching numpy and Excel defaults.

    For q=0.95 on [10,10,...,10,2000,2000,...] with 15 fast + 5 slow samples
    (N=20), this returns ~2000 (the slow tail dominates), whereas
    nearest-rank would return a fast value because rank=ceil(20*0.95)=19
    lands on the last fast sample. Linear interpolation matches what most
    users expect 'p95' to mean.
    """
    if not sorted_ms:
        return 0.0
    if len(sorted_ms) == 1:
        return sorted_ms[0]
    n = len(sorted_ms)
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_ms[lo] + (sorted_ms[hi] - sorted_ms[lo]) * frac


# Sample-size thresholds below which percentile estimates are statistically
# noisy. Reported as advisory notes on each step; don't fail the run.
_MIN_SAMPLES_FOR_P99 = 100
_MIN_SAMPLES_FOR_P95 = 20


def _compute(step: LoadStep, outcomes: list[_Outcome], gates: SloGates) -> StepMetrics:
    requests = len(outcomes)
    successes = sum(1 for o in outcomes if o.success)
    errors = requests - successes
    errors_by_status: dict[int, int] = {}
    for o in outcomes:
        if not o.success:
            errors_by_status[o.status] = errors_by_status.get(o.status, 0) + 1

    latencies = sorted(o.latency_ms for o in outcomes)
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    p99 = _percentile(latencies, 0.99)
    p_max = latencies[-1] if latencies else 0.0
    throughput = requests / step.duration_s

    failed: list[str] = []
    if gates.max_p95_latency_ms is not None and p95 > gates.max_p95_latency_ms:
        failed.append(f"p95 latency {p95:.0f}ms > {gates.max_p95_latency_ms:.0f}ms")
    error_rate = errors / requests if requests else 0.0
    if gates.max_error_rate is not None and error_rate > gates.max_error_rate:
        failed.append(f"error rate {error_rate:.1%} > {gates.max_error_rate:.1%}")
    if gates.min_throughput_rps is not None and throughput < gates.min_throughput_rps:
        failed.append(
            f"throughput {throughput:.1f} rps < {gates.min_throughput_rps:.1f} rps"
        )

    notes: list[str] = []
    if requests < _MIN_SAMPLES_FOR_P95:
        notes.append(
            f"N={requests}: all percentile estimates noisy (need >= {_MIN_SAMPLES_FOR_P95})"
        )
    elif requests < _MIN_SAMPLES_FOR_P99:
        notes.append(
            f"N={requests}: p99 estimate noisy (need >= {_MIN_SAMPLES_FOR_P99} for stable p99)"
        )

    return StepMetrics(
        concurrency=step.concurrency,
        duration_s=step.duration_s,
        requests=requests,
        successes=successes,
        errors=errors,
        errors_by_status=errors_by_status,
        latency_ms_p50=p50,
        latency_ms_p95=p95,
        latency_ms_p99=p99,
        latency_ms_max=p_max,
        throughput_rps=throughput,
        failed_gates=failed,
        notes=notes,
    )


class LoadRunner:
    """Drive a Grok deployment under a step-ramp load profile."""

    def __init__(self, settings: HarnessSettings, log: structlog.BoundLogger):
        self._s = settings
        self._log = log

    async def run(self, suite: TestSuite, profile: LoadProfile) -> LoadReport:
        self._log.info(
            "load.start",
            suite=suite.name,
            steps=len(profile.steps),
            auth_mode=self._s.auth_mode,
        )
        transport = httpx.AsyncHTTPTransport(retries=0, verify=self._s.ssl_context())
        async with httpx.AsyncClient(transport=transport) as http:
            tokens = build_token_provider(self._s, http)
            client = GrokClient(self._s, tokens, http)
            steps_metrics: list[StepMetrics] = []
            for step in profile.steps:
                metrics = await self._run_step(client, suite.cases, step, profile.gates)
                steps_metrics.append(metrics)
                self._log.info(
                    "load.step",
                    concurrency=step.concurrency,
                    requests=metrics.requests,
                    errors=metrics.errors,
                    p95_ms=round(metrics.latency_ms_p95, 1),
                    throughput_rps=round(metrics.throughput_rps, 2),
                    passed=metrics.passed,
                )

        report = LoadReport(
            suite_name=suite.name,
            auth_mode=self._s.auth_mode,
            steps=steps_metrics,
        )
        self._log.info("load.end", passed=report.passed)
        return report

    async def _run_step(
        self,
        client: GrokClient,
        cases: list[TestCase],
        step: LoadStep,
        gates: SloGates,
    ) -> StepMetrics:
        outcomes: list[_Outcome] = []
        stop = asyncio.Event()

        async def _worker(rng: random.Random) -> None:
            while not stop.is_set():
                case = rng.choice(cases)
                start = time.perf_counter()
                try:
                    await client.complete(case)
                    outcomes.append(
                        _Outcome(
                            success=True,
                            latency_ms=(time.perf_counter() - start) * 1000.0,
                            status=200,
                        )
                    )
                except GrokError as e:
                    outcomes.append(
                        _Outcome(
                            success=False,
                            latency_ms=(time.perf_counter() - start) * 1000.0,
                            status=e.status,
                        )
                    )
                except Exception:  # noqa: BLE001
                    outcomes.append(
                        _Outcome(
                            success=False,
                            latency_ms=(time.perf_counter() - start) * 1000.0,
                            status=0,
                        )
                    )
                # Yield so the step timer (and stop.set()) can fire. Real
                # HTTP yields naturally; this matters for fast/mocked transports
                # and costs nothing measurable in production.
                await asyncio.sleep(0)

        # Independent RNG per worker so step IDs are reproducible-ish.
        workers = [
            asyncio.create_task(_worker(random.Random(i + 1)))
            for i in range(step.concurrency)
        ]
        try:
            await asyncio.sleep(step.duration_s)
        finally:
            stop.set()
            await asyncio.gather(*workers, return_exceptions=True)

        return _compute(step, outcomes, gates)
