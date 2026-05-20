"""Tests for the Grok 4.3 Azure OpenAI-compatible client.

Wire shape per Azure OpenAI Service "Chat completions" REST reference:
- POST /openai/deployments/{deployment-id}/chat/completions?api-version=...
- Authorization: Bearer <token>
- Response body has choices[0].message.content, usage.* and finish_reason.
- x-ms-request-id correlates server-side traces.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from grok_harness.auth import BearerToken, FederatedTokenProvider
from grok_harness.client import GrokClient, GrokError
from grok_harness.models import Message, TestCase


class _StubTokens(FederatedTokenProvider):
    def __init__(self, value: str = "stub.bearer"):
        self._v = value

    async def get_token(self) -> BearerToken:
        return BearerToken(value=self._v, expires_at=time.time() + 600)


def _ok_body(content: str = "Paris.") -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 2,
            "total_tokens": 14,
        },
    }


@pytest.mark.asyncio
async def test_happy_path_returns_completion(settings, grok_completions_url):
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(grok_completions_url).mock(
            return_value=httpx.Response(
                200, json=_ok_body(), headers={"x-ms-request-id": "req-abc"}
            )
        )
        async with httpx.AsyncClient() as http:
            client = GrokClient(settings, _StubTokens(), http)
            res = await client.complete(
                TestCase(id="t", messages=[Message(role="user", content="capital of France?")])
            )

    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0] == {"role": "user", "content": "capital of France?"}
    assert sent["max_tokens"] == 1024  # TestCase default
    assert "seed" not in sent
    assert route.calls[0].request.headers["Authorization"] == "Bearer stub.bearer"
    assert "x-ms-client-request-id" in route.calls[0].request.headers

    assert res.content == "Paris."
    assert res.prompt_tokens == 12
    assert res.completion_tokens == 2
    assert res.total_tokens == 14
    assert res.finish_reason == "stop"
    assert res.request_id == "req-abc"
    assert res.latency_ms >= 0


@pytest.mark.asyncio
async def test_seed_and_generation_params_forwarded(settings, grok_completions_url):
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(grok_completions_url).mock(
            return_value=httpx.Response(200, json=_ok_body())
        )
        async with httpx.AsyncClient() as http:
            client = GrokClient(settings, _StubTokens(), http)
            await client.complete(
                TestCase(
                    id="t",
                    messages=[Message(role="user", content="x")],
                    temperature=0.4,
                    top_p=0.9,
                    max_tokens=128,
                    seed=42,
                )
            )

    sent = json.loads(route.calls[0].request.content)
    assert sent["temperature"] == 0.4
    assert sent["top_p"] == 0.9
    assert sent["max_tokens"] == 128
    assert sent["seed"] == 42


@pytest.mark.asyncio
async def test_missing_usage_defaults_to_zero(settings, grok_completions_url):
    """Some Azure deployments omit usage on streaming or filtered responses."""
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    }
    async with respx.mock() as router:
        router.post(grok_completions_url).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            res = await GrokClient(settings, _StubTokens(), http).complete(
                TestCase(id="t", messages=[Message(role="user", content="x")])
            )
    assert res.prompt_tokens == 0
    assert res.completion_tokens == 0
    assert res.total_tokens == 0


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(settings, grok_completions_url):
    async with respx.mock() as router:
        route = router.post(grok_completions_url)
        route.side_effect = [
            httpx.Response(429, json={"error": {"code": "throttled"}}),
            httpx.Response(200, json=_ok_body(content="ok")),
        ]
        async with httpx.AsyncClient() as http:
            res = await GrokClient(settings, _StubTokens(), http).complete(
                TestCase(id="t", messages=[Message(role="user", content="x")])
            )
    assert res.content == "ok"
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_retries_on_503_then_exhausts(settings, grok_completions_url):
    """retry_attempts=2 in the fixture -> 3 total attempts before giving up."""
    async with respx.mock() as router:
        route = router.post(grok_completions_url).mock(
            return_value=httpx.Response(503, json={"error": "ServiceUnavailable"})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(GrokError) as ei:
                await GrokClient(settings, _StubTokens(), http).complete(
                    TestCase(id="t", messages=[Message(role="user", content="x")])
                )
    assert ei.value.status == 503
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_401_is_not_retried(settings, grok_completions_url):
    async with respx.mock() as router:
        route = router.post(grok_completions_url).mock(
            return_value=httpx.Response(
                401,
                json={"error": {"code": "PermissionDenied", "message": "bad token"}},
                headers={"x-ms-request-id": "req-401"},
            )
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(GrokError) as ei:
                await GrokClient(settings, _StubTokens(), http).complete(
                    TestCase(id="t", messages=[Message(role="user", content="x")])
                )
    assert route.call_count == 1
    assert ei.value.status == 401
    assert ei.value.request_id == "req-401"


@pytest.mark.asyncio
async def test_400_content_filter_carries_body(settings, grok_completions_url):
    """Azure returns 400 with code=content_filter when safety blocks the request."""
    payload = {
        "error": {
            "code": "content_filter",
            "message": "The response was filtered.",
            "innererror": {"code": "ResponsibleAIPolicyViolation"},
        }
    }
    async with respx.mock() as router:
        router.post(grok_completions_url).mock(
            return_value=httpx.Response(400, json=payload)
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(GrokError) as ei:
                await GrokClient(settings, _StubTokens(), http).complete(
                    TestCase(id="t", messages=[Message(role="user", content="x")])
                )
    assert ei.value.status == 400
    assert "content_filter" in ei.value.body


@pytest.mark.asyncio
async def test_url_construction_no_double_slash(settings):
    """Pydantic HttpUrl appends '/', client must normalize it away."""
    expected_url = (
        "https://grok-43.eastus2.inference.ml.azure.us/"
        "openai/deployments/grok-4.3/chat/completions"
        "?api-version=2024-12-01-preview"
    )
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(expected_url).mock(
            return_value=httpx.Response(200, json=_ok_body())
        )
        async with httpx.AsyncClient() as http:
            await GrokClient(settings, _StubTokens(), http).complete(
                TestCase(id="t", messages=[Message(role="user", content="x")])
            )
    # respx matches exact URL; if a double slash appeared this would fail.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_grok_error_fields():
    err = GrokError(500, "boom", "req-1")
    assert err.status == 500
    assert err.request_id == "req-1"
    assert "Grok 4.3 returned 500" in str(err)
