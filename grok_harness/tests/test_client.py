"""Integration-shaped unit test: mock the Azure OpenAI surface with respx."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
import respx

from grok_harness.auth import BearerToken, FederatedTokenProvider
from grok_harness.client import GrokClient
from grok_harness.config import AzureSettings, HarnessSettings, KeycloakSettings
from grok_harness.models import Message, TestCase


def _settings(tmp_path: Path) -> HarnessSettings:
    return HarnessSettings(
        keycloak=KeycloakSettings(
            issuer="https://kc.example.gov/realms/il5",
            client_id="grok-harness",
            audience="api://AzureADTokenExchange",
            workload_token_path=tmp_path / "sa-token",
        ),
        azure=AzureSettings(
            tenant_id="00000000-0000-0000-0000-000000000000",
            client_id="11111111-1111-1111-1111-111111111111",
            resource_scope="api://grok-prod/.default",
            authority="https://login.microsoftonline.us",
            endpoint="https://grok-43.eastus2.inference.ml.azure.us",
            deployment="grok-4.3",
        ),
        ca_bundle=None,
        enforce_tls13=False,
        enforce_fips=False,
        audit_log_path=tmp_path / "audit.jsonl",
        request_timeout_s=5.0,
        retry_attempts=1,
    )


class _StubTokens(FederatedTokenProvider):
    def __init__(self):
        pass

    async def get_token(self) -> BearerToken:
        return BearerToken(value="stub.bearer", expires_at=time.time() + 600)


@pytest.mark.asyncio
async def test_grok_chat_completion_happy_path(tmp_path: Path):
    settings = _settings(tmp_path)
    base = str(settings.azure.endpoint).rstrip("/")
    url = (
        f"{base}/openai/deployments/grok-4.3/chat/completions"
        f"?api-version={settings.azure.api_version}"
    )
    async with respx.mock(assert_all_called=True) as router:
        router.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "Paris."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 2,
                        "total_tokens": 14,
                    },
                },
                headers={"x-ms-request-id": "req-abc"},
            )
        )
        async with httpx.AsyncClient() as http:
            client = GrokClient(settings, _StubTokens(), http)
            res = await client.complete(
                TestCase(
                    id="t",
                    messages=[Message(role="user", content="capital of France?")],
                )
            )
    assert res.content == "Paris."
    assert res.completion_tokens == 2
    assert res.request_id == "req-abc"


@pytest.mark.asyncio
async def test_grok_retries_on_429(tmp_path: Path):
    settings = _settings(tmp_path)
    base = str(settings.azure.endpoint).rstrip("/")
    url = (
        f"{base}/openai/deployments/grok-4.3/chat/completions"
        f"?api-version={settings.azure.api_version}"
    )
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(url)
        route.side_effect = [
            httpx.Response(429, json={"error": "throttled"}),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            ),
        ]
        async with httpx.AsyncClient() as http:
            client = GrokClient(settings, _StubTokens(), http)
            res = await client.complete(
                TestCase(id="t", messages=[Message(role="user", content="x")])
            )
    assert res.content == "ok"
    assert route.call_count == 2
