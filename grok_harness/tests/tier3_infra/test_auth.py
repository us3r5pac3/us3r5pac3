"""Tier 3 (infra) — Keycloak -> Azure federation for the bearer token.

Required to call Grok at all, but not the focus of prompt testing. Wire
shapes asserted here come from:

- RFC 8693 (OAuth 2.0 Token Exchange) for the Keycloak leg.
- Microsoft identity platform "Access tokens" / "client credentials with
  certificate" doc for the Azure leg (JWT bearer assertion).
- AKS/GKE projected service-account token semantics (opaque JWT).

Ordered:

  1. Round-trip            wire-shape conformance for both legs
  2. Caching / refresh     reuse within validity; refresh past skew
  3. Failure handling      missing inputs, missing fields, 4xx vs 5xx
  4. Override paths        custom token-exchange endpoint
  5. Helper contract       BearerToken.expired skew
"""
from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from grok_harness.auth import BearerToken, FederatedTokenProvider

pytestmark = pytest.mark.infra


def _form(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


@pytest.mark.asyncio
async def test_two_leg_federation_wire_shape(
    settings, keycloak_token_url, azure_token_url
):
    """End-to-end happy path; assert that each form field matches the spec."""
    async with respx.mock(assert_all_called=True) as router:
        kc = router.post(keycloak_token_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "kc.jwt.assertion",
                    "issued_token_type": "urn:ietf:params:oauth:token-type:jwt",
                    "token_type": "N_A",
                    "expires_in": 300,
                },
            )
        )
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_in": 3599,
                    "access_token": "azure.bearer.value",
                    "ext_expires_in": 3599,
                },
            )
        )
        async with httpx.AsyncClient() as http:
            tok = await FederatedTokenProvider(settings, http).get_token()

    assert isinstance(tok, BearerToken)
    assert tok.value == "azure.bearer.value"
    assert tok.expires_at > time.time() + 3500

    # ---- Keycloak leg: RFC 8693 fields ----
    kc_form = _form(kc.calls[0].request)
    assert kc_form["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert kc_form["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert kc_form["requested_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert kc_form["audience"] == "api://AzureADTokenExchange"
    assert kc_form["client_id"] == "grok-harness"
    # subject_token is the projected SA token verbatim
    assert kc_form["subject_token"].startswith("eyJ")

    # ---- Azure leg: v2.0 client_credentials + JWT bearer assertion ----
    az_form = _form(az.calls[0].request)
    assert az_form["grant_type"] == "client_credentials"
    assert az_form["client_id"] == "11111111-1111-1111-1111-111111111111"
    assert az_form["scope"] == "api://grok-prod/.default"
    assert (
        az_form["client_assertion_type"]
        == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )
    assert az_form["client_assertion"] == "kc.jwt.assertion"


@pytest.mark.asyncio
async def test_token_is_cached_until_near_expiry(
    settings, keycloak_token_url, azure_token_url
):
    """Second call within validity window must not hit either endpoint again."""
    async with respx.mock(assert_all_called=True) as router:
        kc = router.post(keycloak_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "kc1", "expires_in": 300}
            )
        )
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az1", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            prov = FederatedTokenProvider(settings, http)
            t1 = await prov.get_token()
            t2 = await prov.get_token()

    assert t1.value == t2.value == "az1"
    assert kc.call_count == 1
    assert az.call_count == 1


@pytest.mark.asyncio
async def test_token_refresh_when_expired(
    settings, keycloak_token_url, azure_token_url
):
    """When the cached token is past the 60s skew window, the chain refreshes."""
    async with respx.mock(assert_all_called=True) as router:
        kc = router.post(keycloak_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "kc", "expires_in": 300}
            )
        )
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            prov = FederatedTokenProvider(settings, http)
            await prov.get_token()
            # Force expiry by mutating cache.
            prov._cached.expires_at = time.time() - 1  # type: ignore[union-attr]
            await prov.get_token()

    assert kc.call_count == 2
    assert az.call_count == 2


@pytest.mark.asyncio
async def test_missing_workload_token_file_raises(settings):
    settings.keycloak.workload_token_path.unlink()
    async with httpx.AsyncClient() as http:
        prov = FederatedTokenProvider(settings, http)
        with pytest.raises(RuntimeError, match="workload identity token"):
            await prov.get_token()


@pytest.mark.asyncio
async def test_keycloak_response_without_access_token_raises(
    settings, keycloak_token_url
):
    async with respx.mock() as router:
        router.post(keycloak_token_url).mock(
            return_value=httpx.Response(200, json={"error": "invalid_subject_token"})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="no access_token"):
                await FederatedTokenProvider(settings, http).get_token()


@pytest.mark.asyncio
async def test_azure_response_missing_expires_in_raises(
    settings, keycloak_token_url, azure_token_url
):
    async with respx.mock() as router:
        router.post(keycloak_token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "kc"})
        )
        router.post(azure_token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "az"})
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="missing fields"):
                await FederatedTokenProvider(settings, http).get_token()


@pytest.mark.asyncio
async def test_keycloak_4xx_does_not_retry_and_raises(
    settings, keycloak_token_url
):
    async with respx.mock() as router:
        route = router.post(keycloak_token_url).mock(
            return_value=httpx.Response(
                401,
                json={"error": "invalid_grant", "error_description": "subject expired"},
            )
        )
        async with httpx.AsyncClient() as http:
            with pytest.raises(RuntimeError, match="Auth failure 401"):
                await FederatedTokenProvider(settings, http).get_token()
        # Single attempt, no retry, since 401 is a definitive auth rejection.
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_azure_5xx_retries_then_succeeds(
    settings, keycloak_token_url, azure_token_url
):
    async with respx.mock() as router:
        router.post(keycloak_token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "kc", "expires_in": 60})
        )
        az = router.post(azure_token_url)
        az.side_effect = [
            httpx.Response(503, json={"error": "ServiceUnavailable"}),
            httpx.Response(
                200,
                json={"access_token": "az.after.retry", "expires_in": 3600},
            ),
        ]
        async with httpx.AsyncClient() as http:
            tok = await FederatedTokenProvider(settings, http).get_token()

    assert tok.value == "az.after.retry"
    assert az.call_count == 2


@pytest.mark.asyncio
async def test_custom_token_exchange_endpoint_is_honored(
    settings, azure_token_url
):
    override = "https://kc.example.gov/realms/il5/protocol/openid-connect/ext/token"
    settings.keycloak.token_exchange_endpoint = override
    async with respx.mock(assert_all_called=True) as router:
        kc = router.post(override).mock(
            return_value=httpx.Response(
                200, json={"access_token": "kc", "expires_in": 300}
            )
        )
        router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            await FederatedTokenProvider(settings, http).get_token()

    assert kc.call_count == 1


@pytest.mark.asyncio
async def test_bearer_token_expired_helper():
    near = BearerToken(value="x", expires_at=time.time() + 30)  # within 60s skew
    far = BearerToken(value="x", expires_at=time.time() + 600)
    assert near.expired is True
    assert far.expired is False
