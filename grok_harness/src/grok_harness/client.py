from __future__ import annotations

import time
import uuid

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from .auth import TokenProvider
from .config import HarnessSettings
from .models import CompletionResult, TestCase


class GrokError(RuntimeError):
    def __init__(self, status: int, body: str, request_id: str | None):
        super().__init__(f"Grok 4.3 returned {status}: {body[:512]}")
        self.status = status
        self.body = body
        self.request_id = request_id


def _make_is_retryable(statuses: tuple[int, ...]):
    """Build a retry predicate bound to the configured retryable statuses."""
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, GrokError):
            return exc.status in statuses
        return isinstance(
            exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)
        )
    return _is_retryable


class GrokClient:
    """Chat-completions client for Grok 4.3 hosted on Azure Government.

    Uses the Azure AI Inference REST shape (OpenAI-compatible /chat/completions).
    Token acquisition is delegated to the federation provider so each request
    carries a fresh Azure-issued bearer.
    """

    def __init__(
        self,
        settings: HarnessSettings,
        tokens: TokenProvider,
        http: httpx.AsyncClient,
    ):
        self._s = settings
        self._tokens = tokens
        self._http = http

    async def complete(self, case: TestCase) -> CompletionResult:
        url = self._s.azure.url_template.format(
            endpoint=str(self._s.azure.endpoint).rstrip("/"),
            deployment=self._s.azure.deployment,
            api_version=self._s.azure.api_version,
        )
        body = {
            "messages": [m.model_dump() for m in case.messages],
            "temperature": case.temperature,
            "top_p": case.top_p,
            "max_tokens": case.max_tokens,
        }
        if case.seed is not None:
            body["seed"] = case.seed

        client_request_id = str(uuid.uuid4())

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.retry_attempts + 1),
            wait=wait_exponential_jitter(
                initial=self._s.backoff_initial_s,
                max=self._s.backoff_max_s,
            ),
            retry=retry_if_exception(_make_is_retryable(self._s.retry_statuses)),
            reraise=True,
        ):
            with attempt:
                token = await self._tokens.get_token()
                headers = {
                    "Content-Type": "application/json",
                    "x-ms-client-request-id": client_request_id,
                }
                token.apply_to(headers)
                start = time.perf_counter()
                resp = await self._http.post(
                    url, json=body, headers=headers, timeout=self._s.request_timeout_s
                )
                latency_ms = (time.perf_counter() - start) * 1000.0
                request_id = resp.headers.get("x-ms-request-id") or client_request_id

                if resp.status_code >= 400:
                    raise GrokError(resp.status_code, resp.text, request_id)

                data = resp.json()
                choice = data["choices"][0]
                usage = data.get("usage", {})
                return CompletionResult(
                    content=choice["message"]["content"] or "",
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                    total_tokens=int(usage.get("total_tokens", 0)),
                    latency_ms=latency_ms,
                    finish_reason=choice.get("finish_reason"),
                    request_id=request_id,
                )
        raise RuntimeError("unreachable")
