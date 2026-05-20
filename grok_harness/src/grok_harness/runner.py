from __future__ import annotations

import asyncio
import uuid

import httpx
import structlog

from .auth import FederatedTokenProvider
from .client import GrokClient, GrokError
from .config import HarnessSettings
from .evaluators import evaluate
from .models import CaseOutcome, TestCase, TestSuite


class SuiteRunner:
    def __init__(self, settings: HarnessSettings, log: structlog.BoundLogger):
        self._s = settings
        self._log = log

    async def run(self, suite: TestSuite) -> list[CaseOutcome]:
        run_id = str(uuid.uuid4())
        self._log.info(
            "suite.start",
            run_id=run_id,
            suite=suite.name,
            cases=len(suite.cases),
            endpoint=str(self._s.azure.endpoint),
            deployment=self._s.azure.deployment,
        )

        sem = asyncio.Semaphore(self._s.max_concurrency)
        transport = httpx.AsyncHTTPTransport(
            retries=0,
            verify=self._s.ssl_context(),
        )
        async with httpx.AsyncClient(transport=transport) as http:
            tokens = FederatedTokenProvider(self._s, http)
            client = GrokClient(self._s, tokens, http)

            async def _one(case: TestCase) -> CaseOutcome:
                async with sem:
                    return await self._run_case(client, run_id, suite.name, case)

            outcomes = await asyncio.gather(*(_one(c) for c in suite.cases))

        passed = sum(1 for o in outcomes if o.passed)
        self._log.info(
            "suite.end",
            run_id=run_id,
            suite=suite.name,
            passed=passed,
            failed=len(outcomes) - passed,
        )
        return outcomes

    async def _run_case(
        self,
        client: GrokClient,
        run_id: str,
        suite_name: str,
        case: TestCase,
    ) -> CaseOutcome:
        case_log = self._log.bind(run_id=run_id, suite=suite_name, case_id=case.id)
        case_log.info("case.start", tags=case.tags)
        try:
            completion = await client.complete(case)
        except GrokError as e:
            case_log.error(
                "case.error",
                status=e.status,
                request_id=e.request_id,
                error=str(e),
            )
            return CaseOutcome(
                case_id=case.id,
                tags=case.tags,
                passed=False,
                completion=None,
                assertions=[],
                error=str(e),
            )
        except Exception as e:  # noqa: BLE001
            case_log.error("case.error", error=str(e), error_type=type(e).__name__)
            return CaseOutcome(
                case_id=case.id,
                tags=case.tags,
                passed=False,
                completion=None,
                assertions=[],
                error=str(e),
            )

        outcomes = [evaluate(a, completion) for a in case.assertions]
        passed = all(o.passed for o in outcomes)

        case_log.info(
            "case.end",
            passed=passed,
            latency_ms=round(completion.latency_ms, 1),
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            request_id=completion.request_id,
            response=completion.content,  # redacted to hash if configured
            failed_assertions=[
                {"kind": o.assertion.kind, "detail": o.detail}
                for o in outcomes
                if not o.passed
            ],
        )

        return CaseOutcome(
            case_id=case.id,
            tags=case.tags,
            passed=passed,
            completion=completion,
            assertions=outcomes,
        )
