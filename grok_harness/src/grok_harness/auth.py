from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import HarnessSettings


class _AuthRejection(RuntimeError):
    """Non-retryable auth failure (4xx from the IdP or Azure AD)."""


def _is_retryable_auth(exc: BaseException) -> bool:
    if isinstance(exc, _AuthRejection):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(
        exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)
    )


@dataclass(slots=True)
class BearerToken:
    value: str
    expires_at: float  # epoch seconds
    scheme: str = "Bearer"  # "Bearer" or "api-key"
    expiry_skew_s: float = 60.0

    @property
    def expired(self) -> bool:
        # Treat the token as expired this many seconds early so a request
        # in flight never carries a token that's about to die server-side.
        return time.time() >= (self.expires_at - self.expiry_skew_s)

    def apply_to(self, headers: dict[str, str]) -> None:
        """Render this credential into outgoing request headers."""
        if self.scheme.lower() == "api-key":
            headers["api-key"] = self.value
        else:
            headers["Authorization"] = f"{self.scheme} {self.value}"


class TokenProvider(Protocol):
    """Contract every auth mode implements."""

    async def get_token(self) -> BearerToken: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _RetryingHTTP:
    """Mixin for token providers that do retried POSTs/GETs against an IdP."""

    def __init__(self, settings: HarnessSettings, http: httpx.AsyncClient):
        self._s = settings
        self._http = http
        self._cached: BearerToken | None = None

    async def _retrying_request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.retry_attempts + 1),
            wait=wait_exponential_jitter(
                initial=self._s.backoff_initial_s,
                max=self._s.backoff_max_s,
            ),
            retry=retry_if_exception(_is_retryable_auth),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.request(
                    method,
                    url,
                    data=data,
                    params=params,
                    headers={"Accept": "application/json", **(headers or {})},
                    timeout=self._s.request_timeout_s,
                )
                if resp.status_code >= 500:
                    resp.raise_for_status()
                if resp.status_code >= 400:
                    raise _AuthRejection(
                        f"Auth failure {resp.status_code} at {url}: {resp.text[:512]}"
                    )
                return resp
        raise RuntimeError("unreachable")


def _azure_token_url(settings: HarnessSettings) -> str:
    az = settings.azure
    return f"{str(az.authority).rstrip('/')}/{az.tenant_id}/oauth2/v2.0/token"


def _resource_uri(scope: str) -> str:
    """Convert a v2 scope (api://app/.default) to a v1 resource URI."""
    return scope.removesuffix("/.default")


# ---------------------------------------------------------------------------
# Auth mode 1 — Keycloak federation (default; IL5 GCP -> Azure)
# ---------------------------------------------------------------------------


class FederatedTokenProvider(_RetryingHTTP):
    """Two-leg federation: workload SA token -> Keycloak JWT -> Azure bearer.

    Used when the caller lives outside Azure (e.g. a GCP/GKE pod) and an
    external IdP (Keycloak) bridges the agency's workload identity to
    Azure AD via a federated credential on the App Registration.
    """

    async def get_token(self) -> BearerToken:
        if self._cached and not self._cached.expired:
            return self._cached
        kc_jwt = await self._fetch_keycloak_assertion()
        azure_token = await self._exchange_for_azure_token(kc_jwt)
        self._cached = azure_token
        return azure_token

    def _read_workload_token(self) -> str:
        path = self._s.keycloak.workload_token_path
        try:
            return path.read_text().strip()
        except OSError as e:
            raise RuntimeError(f"Cannot read workload identity token at {path}: {e}") from e

    async def _fetch_keycloak_assertion(self) -> str:
        kc = self._s.keycloak
        if kc.token_exchange_endpoint is not None:
            endpoint = str(kc.token_exchange_endpoint)
        else:
            endpoint = f"{str(kc.issuer).rstrip('/')}/protocol/openid-connect/token"
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": kc.client_id,
            "subject_token": self._read_workload_token(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "requested_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "audience": kc.audience,
            "scope": kc.scope,
        }
        resp = await self._retrying_request("POST", endpoint, data=data)
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"Keycloak token exchange returned no access_token: {payload}")
        return token

    async def _exchange_for_azure_token(self, kc_assertion: str) -> BearerToken:
        az = self._s.azure
        data = {
            "grant_type": "client_credentials",
            "client_id": az.client_id,
            "scope": az.resource_scope,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": kc_assertion,
        }
        resp = await self._retrying_request("POST", _azure_token_url(self._s), data=data)
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise RuntimeError(f"Azure AD token response missing fields: {payload}")
        return BearerToken(
            value=token,
            expires_at=time.time() + expires_in,
            expiry_skew_s=self._s.token_expiry_skew_s,
        )


# ---------------------------------------------------------------------------
# Auth mode 2 — Service-principal client_secret (simplest for Azure team)
# ---------------------------------------------------------------------------


class ClientSecretTokenProvider(_RetryingHTTP):
    """Classic Azure AD service principal with a shared secret.

    Easiest path for ad-hoc validation from inside Azure. NOT IL5: secrets
    on disk / in env violate the prod boundary. Use for dev/CI only.
    """

    async def get_token(self) -> BearerToken:
        if self._cached and not self._cached.expired:
            return self._cached
        az = self._s.azure
        if not az.client_secret:
            raise RuntimeError(
                "auth_mode=client_secret requires GH_AZURE_CLIENT_SECRET"
            )
        data = {
            "grant_type": "client_credentials",
            "client_id": az.client_id,
            "client_secret": az.client_secret.get_secret_value(),
            "scope": az.resource_scope,
        }
        resp = await self._retrying_request("POST", _azure_token_url(self._s), data=data)
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise RuntimeError(f"Azure AD token response missing fields: {payload}")
        self._cached = BearerToken(
            value=token,
            expires_at=time.time() + expires_in,
            expiry_skew_s=self._s.token_expiry_skew_s,
        )
        return self._cached


# ---------------------------------------------------------------------------
# Auth mode 3 — Managed Identity via IMDS (Azure VMs / App Service / older AKS)
# ---------------------------------------------------------------------------


class ManagedIdentityTokenProvider(_RetryingHTTP):
    """Azure IMDS managed identity (system or user-assigned).

    The host's assigned identity is exchanged at the Instance Metadata
    Service for a bearer scoped to the Grok resource. No secrets required.
    Works in Azure VMs, App Service, Container Instances, and AKS pods
    with the legacy aad-pod-identity sidecar.
    """

    IMDS_ENDPOINT = "http://169.254.169.254/metadata/identity/oauth2/token"

    async def get_token(self) -> BearerToken:
        if self._cached and not self._cached.expired:
            return self._cached
        params = {
            "api-version": self._s.imds_api_version,
            "resource": _resource_uri(self._s.azure.resource_scope),
        }
        if self._s.azure.managed_identity_client_id:
            params["client_id"] = self._s.azure.managed_identity_client_id

        endpoint = self._s.azure.imds_endpoint or self.IMDS_ENDPOINT
        resp = await self._retrying_request(
            "GET", endpoint, params=params, headers={"Metadata": "true"}
        )
        payload = resp.json()
        token = payload.get("access_token")
        expires_in_raw = payload.get("expires_in") or payload.get("expires_on")
        if not token or not expires_in_raw:
            raise RuntimeError(f"IMDS response missing fields: {payload}")
        # IMDS sometimes returns expires_in (relative) or expires_on (absolute);
        # if absolute, derive seconds-until.
        expires_in = int(expires_in_raw)
        now = time.time()
        if expires_in > now:
            expires_at = float(expires_in)
        else:
            expires_at = now + expires_in
        self._cached = BearerToken(
            value=token,
            expires_at=expires_at,
            expiry_skew_s=self._s.token_expiry_skew_s,
        )
        return self._cached


# ---------------------------------------------------------------------------
# Auth mode 4 — Azure AD Workload Identity (modern AKS)
# ---------------------------------------------------------------------------


class AzureWorkloadIdentityTokenProvider(_RetryingHTTP):
    """AKS Workload Identity (azwi).

    The pod has a projected Azure AD token at a well-known path
    (/var/run/secrets/azure/tokens/azure-identity-token by default).
    That token IS the client_assertion for Azure AD — no Keycloak hop.
    """

    DEFAULT_TOKEN_PATH = Path("/var/run/secrets/azure/tokens/azure-identity-token")

    async def get_token(self) -> BearerToken:
        if self._cached and not self._cached.expired:
            return self._cached
        token_path = self._s.azure.workload_token_path or self.DEFAULT_TOKEN_PATH
        try:
            assertion = token_path.read_text().strip()
        except OSError as e:
            raise RuntimeError(
                f"Cannot read Azure workload-identity token at {token_path}: {e}"
            ) from e

        az = self._s.azure
        data = {
            "grant_type": "client_credentials",
            "client_id": az.client_id,
            "scope": az.resource_scope,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": assertion,
        }
        resp = await self._retrying_request("POST", _azure_token_url(self._s), data=data)
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise RuntimeError(f"Azure AD token response missing fields: {payload}")
        self._cached = BearerToken(
            value=token,
            expires_at=time.time() + expires_in,
            expiry_skew_s=self._s.token_expiry_skew_s,
        )
        return self._cached


# ---------------------------------------------------------------------------
# Auth mode 5 — Static bearer (testing, scripted CI with pre-fetched tokens)
# ---------------------------------------------------------------------------


class StaticBearerTokenProvider:
    """Use a bearer the operator obtained out-of-band (e.g. `az account get-access-token`).

    No refresh: when this token expires, the harness fails fast. Intended
    for short interactive runs and as a dev escape hatch.
    """

    def __init__(self, settings: HarnessSettings, http: httpx.AsyncClient):
        self._s = settings
        # http kept for protocol parity; unused.
        del http

    async def get_token(self) -> BearerToken:
        bearer = self._s.azure.static_bearer
        if not bearer:
            raise RuntimeError(
                "auth_mode=static_bearer requires GH_AZURE_STATIC_BEARER"
            )
        # Optimistic validity window; configurable via GH_STATIC_BEARER_TTL_S.
        # The client will see 401s and fail if the operator's token has
        # already expired.
        return BearerToken(
            value=bearer.get_secret_value(),
            expires_at=time.time() + self._s.static_bearer_ttl_s,
            expiry_skew_s=self._s.token_expiry_skew_s,
        )


# ---------------------------------------------------------------------------
# Auth mode 6 — Plain API key (DEV ONLY; commercial Azure-hosted Grok endpoint)
# ---------------------------------------------------------------------------


class ApiKeyTokenProvider:
    """Static API key against a commercial Azure-hosted Grok endpoint.

    Use case: the Grok team iterating against an Azure AI Foundry MaaS
    deployment before the federated path is plumbed. The key is sent as
    the `api-key` header (Azure OpenAI / Foundry convention), not as a
    Bearer.

    NOT IL5-compliant. The key lives in env or Key Vault; there is no
    rotation in-band and no expiry signal. Always pair with
    GH_ENFORCE_FIPS=false and a commercial-cloud GH_GROK_ENDPOINT.
    """

    def __init__(self, settings: HarnessSettings, http: httpx.AsyncClient):
        self._s = settings
        del http  # protocol parity; no HTTP needed

    async def get_token(self) -> BearerToken:
        api_key = self._s.azure.api_key
        if not api_key:
            raise RuntimeError("auth_mode=api_key requires GH_AZURE_API_KEY")
        # Cache TTL is configurable via GH_API_KEY_TTL_S; API keys are
        # long-lived and the client sees 401 if rotated server-side.
        return BearerToken(
            value=api_key.get_secret_value(),
            expires_at=time.time() + self._s.api_key_ttl_s,
            scheme="api-key",
            expiry_skew_s=self._s.token_expiry_skew_s,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_token_provider(settings: HarnessSettings, http: httpx.AsyncClient) -> TokenProvider:
    mode = settings.auth_mode
    if mode == "keycloak_federated":
        return FederatedTokenProvider(settings, http)
    if mode == "client_secret":
        return ClientSecretTokenProvider(settings, http)
    if mode == "managed_identity":
        return ManagedIdentityTokenProvider(settings, http)
    if mode == "azure_workload_identity":
        return AzureWorkloadIdentityTokenProvider(settings, http)
    if mode == "static_bearer":
        return StaticBearerTokenProvider(settings, http)
    if mode == "api_key":
        return ApiKeyTokenProvider(settings, http)
    raise ValueError(f"unknown auth_mode: {mode!r}")
