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

from grok_harness.auth import (
    ApiKeyTokenProvider,
    AzureWorkloadIdentityTokenProvider,
    BearerToken,
    ClientSecretTokenProvider,
    FederatedTokenProvider,
    ManagedIdentityTokenProvider,
    StaticBearerTokenProvider,
    build_token_provider,
)

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


# ============================================================================
# Auth mode: client_secret (Azure team simplest path)
# ============================================================================


@pytest.mark.asyncio
async def test_client_secret_provider_wire_shape(settings, azure_token_url):
    from pydantic import SecretStr

    settings.auth_mode = "client_secret"
    settings.azure.client_secret = SecretStr("super.secret.value")

    async with respx.mock(assert_all_called=True) as router:
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "access_token": "azure.bearer.value",
                    "expires_in": 3599,
                },
            )
        )
        async with httpx.AsyncClient() as http:
            tok = await ClientSecretTokenProvider(settings, http).get_token()

    assert tok.value == "azure.bearer.value"
    form = _form(az.calls[0].request)
    assert form["grant_type"] == "client_credentials"
    assert form["client_id"] == "11111111-1111-1111-1111-111111111111"
    assert form["client_secret"] == "super.secret.value"
    assert form["scope"] == "api://grok-prod/.default"


@pytest.mark.asyncio
async def test_client_secret_provider_requires_secret(settings):
    settings.auth_mode = "client_secret"
    settings.azure.client_secret = None
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="GH_AZURE_CLIENT_SECRET"):
            await ClientSecretTokenProvider(settings, http).get_token()


@pytest.mark.asyncio
async def test_client_secret_provider_caches(settings, azure_token_url):
    from pydantic import SecretStr

    settings.azure.client_secret = SecretStr("s")
    async with respx.mock() as router:
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            prov = ClientSecretTokenProvider(settings, http)
            await prov.get_token()
            await prov.get_token()
    assert az.call_count == 1


# ============================================================================
# Auth mode: managed_identity (IMDS)
# ============================================================================


@pytest.mark.asyncio
async def test_managed_identity_provider_wire_shape(settings):
    """IMDS contract:

      GET http://169.254.169.254/metadata/identity/oauth2/token
        ?api-version=2018-02-01&resource={resource_uri}
      Headers: Metadata: true
    """
    settings.auth_mode = "managed_identity"
    imds = ManagedIdentityTokenProvider.IMDS_ENDPOINT

    async with respx.mock(assert_all_called=True) as router:
        route = router.get(imds).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "mi.bearer",
                    "expires_in": 3599,
                    "resource": "api://grok-prod",
                    "token_type": "Bearer",
                },
            )
        )
        async with httpx.AsyncClient() as http:
            tok = await ManagedIdentityTokenProvider(settings, http).get_token()

    assert tok.value == "mi.bearer"

    req = route.calls[0].request
    assert req.headers["Metadata"] == "true"
    # resource is the v1-style URI, /.default suffix stripped.
    assert req.url.params["resource"] == "api://grok-prod"
    assert req.url.params["api-version"] == "2018-02-01"
    # No user-assigned client_id by default.
    assert "client_id" not in req.url.params


@pytest.mark.asyncio
async def test_managed_identity_user_assigned_client_id(settings):
    settings.azure.managed_identity_client_id = "22222222-2222-2222-2222-222222222222"
    imds = ManagedIdentityTokenProvider.IMDS_ENDPOINT
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(imds).mock(
            return_value=httpx.Response(
                200, json={"access_token": "mi", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            await ManagedIdentityTokenProvider(settings, http).get_token()
    assert (
        route.calls[0].request.url.params["client_id"]
        == "22222222-2222-2222-2222-222222222222"
    )


@pytest.mark.asyncio
async def test_managed_identity_imds_endpoint_override(settings):
    """Azure App Service exposes IDENTITY_ENDPOINT instead of the IMDS IP."""
    settings.azure.imds_endpoint = "http://127.0.0.1:42356/msi/token"
    async with respx.mock(assert_all_called=True) as router:
        route = router.get("http://127.0.0.1:42356/msi/token").mock(
            return_value=httpx.Response(
                200, json={"access_token": "mi", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            await ManagedIdentityTokenProvider(settings, http).get_token()
    assert route.call_count == 1


# ============================================================================
# Auth mode: azure_workload_identity (AKS azwi)
# ============================================================================


@pytest.mark.asyncio
async def test_workload_identity_uses_projected_token_as_assertion(
    settings, tmp_path, azure_token_url
):
    """AKS pods get an Azure-issued projected token; it IS the client_assertion."""
    projected = tmp_path / "azure-identity-token"
    projected.write_text("eyJ.projected.azure.assertion")
    settings.azure.workload_token_path = projected
    settings.auth_mode = "azure_workload_identity"

    async with respx.mock(assert_all_called=True) as router:
        az = router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az.bearer", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            tok = await AzureWorkloadIdentityTokenProvider(settings, http).get_token()

    assert tok.value == "az.bearer"
    form = _form(az.calls[0].request)
    assert form["grant_type"] == "client_credentials"
    assert form["client_assertion"] == "eyJ.projected.azure.assertion"
    assert (
        form["client_assertion_type"]
        == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    )


@pytest.mark.asyncio
async def test_workload_identity_missing_token_file_raises(settings, tmp_path):
    settings.azure.workload_token_path = tmp_path / "does-not-exist"
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="workload-identity token"):
            await AzureWorkloadIdentityTokenProvider(settings, http).get_token()


# ============================================================================
# Auth mode: static_bearer (interactive dev)
# ============================================================================


@pytest.mark.asyncio
async def test_static_bearer_returns_configured_token(settings):
    from pydantic import SecretStr

    settings.azure.static_bearer = SecretStr("manually-acquired-token")
    async with httpx.AsyncClient() as http:
        tok = await StaticBearerTokenProvider(settings, http).get_token()
    assert tok.value == "manually-acquired-token"


@pytest.mark.asyncio
async def test_static_bearer_requires_value(settings):
    settings.azure.static_bearer = None
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="GH_AZURE_STATIC_BEARER"):
            await StaticBearerTokenProvider(settings, http).get_token()


# ============================================================================
# Factory dispatch
# ============================================================================


# ============================================================================
# Auth mode: api_key (dev-only against a commercial Azure-hosted endpoint)
# ============================================================================


@pytest.mark.asyncio
async def test_api_key_provider_returns_token_with_apikey_scheme(settings):
    from pydantic import SecretStr

    settings.azure.api_key = SecretStr("sk-abc123")
    async with httpx.AsyncClient() as http:
        tok = await ApiKeyTokenProvider(settings, http).get_token()
    assert tok.value == "sk-abc123"
    assert tok.scheme == "api-key"
    # API keys cache long; no in-band rotation signal.
    assert not tok.expired


@pytest.mark.asyncio
async def test_api_key_provider_requires_value(settings):
    settings.azure.api_key = None
    async with httpx.AsyncClient() as http:
        with pytest.raises(RuntimeError, match="GH_AZURE_API_KEY"):
            await ApiKeyTokenProvider(settings, http).get_token()


# ============================================================================
# BearerToken header rendering — Bearer vs api-key
# ============================================================================


def test_bearer_token_applies_authorization_header_by_default():
    headers: dict[str, str] = {}
    BearerToken(value="abc", expires_at=time.time() + 600).apply_to(headers)
    assert headers == {"Authorization": "Bearer abc"}


def test_bearer_token_renders_api_key_header_for_apikey_scheme():
    headers: dict[str, str] = {}
    BearerToken(value="sk-abc", expires_at=time.time() + 600, scheme="api-key").apply_to(
        headers
    )
    assert headers == {"api-key": "sk-abc"}
    assert "Authorization" not in headers


# ============================================================================
# Factory dispatch
# ============================================================================


@pytest.mark.asyncio
async def test_build_token_provider_dispatches_each_mode(settings):
    from pydantic import SecretStr

    settings.azure.client_secret = SecretStr("s")
    settings.azure.static_bearer = SecretStr("b")
    settings.azure.api_key = SecretStr("k")

    async with httpx.AsyncClient() as http:
        settings.auth_mode = "keycloak_federated"
        assert isinstance(build_token_provider(settings, http), FederatedTokenProvider)
        settings.auth_mode = "client_secret"
        assert isinstance(build_token_provider(settings, http), ClientSecretTokenProvider)
        settings.auth_mode = "managed_identity"
        assert isinstance(
            build_token_provider(settings, http), ManagedIdentityTokenProvider
        )
        settings.auth_mode = "azure_workload_identity"
        assert isinstance(
            build_token_provider(settings, http), AzureWorkloadIdentityTokenProvider
        )
        settings.auth_mode = "static_bearer"
        assert isinstance(build_token_provider(settings, http), StaticBearerTokenProvider)
        settings.auth_mode = "api_key"
        assert isinstance(build_token_provider(settings, http), ApiKeyTokenProvider)


@pytest.mark.asyncio
async def test_build_token_provider_rejects_unknown_mode(settings):
    settings.auth_mode = "moonbeam"  # type: ignore[assignment]
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="unknown auth_mode"):
            build_token_provider(settings, http)


# ============================================================================
# Parameterization — verify the cross-cutting knobs actually take effect.
# ============================================================================


@pytest.mark.asyncio
async def test_token_expiry_skew_is_threaded_into_bearer_token(
    settings, azure_token_url
):
    """Every provider must construct BearerToken with the configured skew."""
    from pydantic import SecretStr

    settings.token_expiry_skew_s = 300.0  # 5 minutes
    settings.azure.client_secret = SecretStr("s")

    async with respx.mock() as router:
        router.post(azure_token_url).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            tok = await ClientSecretTokenProvider(settings, http).get_token()

    assert tok.expiry_skew_s == 300.0


@pytest.mark.asyncio
async def test_static_bearer_ttl_is_configurable(settings):
    from pydantic import SecretStr

    settings.static_bearer_ttl_s = 1800  # 30 minutes
    settings.azure.static_bearer = SecretStr("eyJ.token")
    async with httpx.AsyncClient() as http:
        tok = await StaticBearerTokenProvider(settings, http).get_token()
    # Token must be valid ~30 minutes from now (allow a few seconds slack).
    assert 1750 < (tok.expires_at - time.time()) <= 1800


@pytest.mark.asyncio
async def test_api_key_ttl_is_configurable(settings):
    from pydantic import SecretStr

    settings.api_key_ttl_s = 3600  # 1 hour
    settings.azure.api_key = SecretStr("sk-xyz")
    async with httpx.AsyncClient() as http:
        tok = await ApiKeyTokenProvider(settings, http).get_token()
    assert 3550 < (tok.expires_at - time.time()) <= 3600


@pytest.mark.asyncio
async def test_imds_api_version_is_configurable(settings):
    """Setting GH_IMDS_API_VERSION must change the api-version query parameter."""
    settings.imds_api_version = "2019-08-01"
    imds = ManagedIdentityTokenProvider.IMDS_ENDPOINT

    async with respx.mock(assert_all_called=True) as router:
        route = router.get(imds).mock(
            return_value=httpx.Response(
                200, json={"access_token": "mi", "expires_in": 3600}
            )
        )
        async with httpx.AsyncClient() as http:
            await ManagedIdentityTokenProvider(settings, http).get_token()

    assert route.calls[0].request.url.params["api-version"] == "2019-08-01"


@pytest.mark.asyncio
async def test_backoff_settings_flow_into_auth_retry(settings, keycloak_token_url):
    """Tiny backoff lets a 5xx-then-200 chain complete quickly under test."""
    settings.backoff_initial_s = 0.01
    settings.backoff_max_s = 0.02
    settings.retry_attempts = 2

    async with respx.mock() as router:
        route = router.post(keycloak_token_url)
        route.side_effect = [
            httpx.Response(503, json={"error": "x"}),
            httpx.Response(200, json={"access_token": "kc", "expires_in": 60}),
        ]
        # Stub the Azure leg so the chain completes.
        router.post(_azure_token_url_for(settings)).mock(
            return_value=httpx.Response(
                200, json={"access_token": "az", "expires_in": 3600}
            )
        )
        start = time.perf_counter()
        async with httpx.AsyncClient() as http:
            await FederatedTokenProvider(settings, http).get_token()
        elapsed = time.perf_counter() - start

    # With ~10-20ms backoff between attempts, total elapsed must be << 1s.
    assert elapsed < 1.0
    assert route.call_count == 2


def _azure_token_url_for(settings) -> str:
    return (
        f"{str(settings.azure.authority).rstrip('/')}/"
        f"{settings.azure.tenant_id}/oauth2/v2.0/token"
    )


def test_bearer_token_skew_field_drives_expired_property():
    """A 5-minute skew means a token expiring in 2 minutes already reads expired."""
    near = BearerToken(value="x", expires_at=time.time() + 120, expiry_skew_s=300)
    far = BearerToken(value="x", expires_at=time.time() + 120, expiry_skew_s=60)
    assert near.expired is True
    assert far.expired is False
