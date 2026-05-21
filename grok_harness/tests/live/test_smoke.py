"""Live smoke: one auth round-trip + one prompt round-trip.

Both tests use whatever GH_AUTH_MODE is set in the live environment.
Each prints a short line so the operator can see what was exercised.
"""
from __future__ import annotations

import os

import httpx
import pytest

from grok_harness.auth import build_token_provider
from grok_harness.client import GrokClient
from grok_harness.config import load_from_env
from grok_harness.models import Message, TestCase

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GH_LIVE"),
        reason="set GH_LIVE=1 to run live tests against real services",
    ),
]


@pytest.mark.asyncio
async def test_live_auth_obtains_token(capsys):
    """Step 1: the configured auth_mode can mint a non-empty bearer."""
    settings = load_from_env()
    transport = httpx.AsyncHTTPTransport(retries=0, verify=settings.ssl_context())
    async with httpx.AsyncClient(transport=transport) as http:
        provider = build_token_provider(settings, http)
        token = await provider.get_token()

    assert token.value, "auth provider returned an empty token"
    assert not token.expired, "auth provider returned an already-expired token"

    # Don't print the token; print enough for the operator to see the path.
    print(
        f"[live] auth_mode={settings.auth_mode} "
        f"scheme={token.scheme} ttl={token.expires_at - __import__('time').time():.0f}s"
    )


@pytest.mark.asyncio
async def test_live_prompt_round_trip(capsys):
    """Step 2: a minimal prompt actually reaches Grok and returns content."""
    settings = load_from_env()
    transport = httpx.AsyncHTTPTransport(retries=0, verify=settings.ssl_context())
    async with httpx.AsyncClient(transport=transport) as http:
        provider = build_token_provider(settings, http)
        client = GrokClient(settings, provider, http)
        case = TestCase(
            id="live-smoke",
            messages=[Message(role="user", content="Reply with the single word: OK")],
            temperature=0.0,
            max_tokens=8,
        )
        result = await client.complete(case)

    assert result.content, "Grok returned an empty response body"
    assert result.prompt_tokens > 0, "usage.prompt_tokens missing or zero"
    assert result.request_id, "x-ms-request-id missing"

    print(
        f"[live] endpoint={settings.azure.endpoint} "
        f"deployment={settings.azure.deployment} "
        f"latency_ms={result.latency_ms:.0f} "
        f"prompt_tok={result.prompt_tokens} "
        f"completion_tok={result.completion_tokens} "
        f"finish={result.finish_reason} "
        f"req_id={result.request_id}"
    )
