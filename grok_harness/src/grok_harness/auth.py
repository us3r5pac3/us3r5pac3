from __future__ import annotations

import time
from dataclasses import dataclass

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

    @property
    def expired(self) -> bool:
        # 60s skew so we never hand out a token about to expire mid-flight.
        return time.time() >= (self.expires_at - 60)


class FederatedTokenProvider:
    """Two-leg federation: workload SA token -> Keycloak JWT -> Azure access token.

    Flow:

      1. Read the projected K8s service account token from disk. In IL5
         deployments the SA is bound to a SPIFFE ID; Keycloak trusts the
         cluster's OIDC issuer.
      2. POST to Keycloak's token endpoint with RFC 8693 token-exchange
         (subject_token=SA token, audience=Azure). Keycloak issues a JWT
         that names AzureADTokenExchange as audience.
      3. POST to Azure AD with grant_type=client_credentials and
         client_assertion=<keycloak JWT>. Azure validates the JWT against
         Keycloak's JWKS (federated credential on the App Registration)
         and returns a bearer scoped to the Grok resource.

    The Azure token is cached until ~1 minute before expiry.
    """

    def __init__(self, settings: HarnessSettings, http: httpx.AsyncClient):
        self._s = settings
        self._http = http
        self._cached: BearerToken | None = None

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
        resp = await self._retrying_post(endpoint, data)
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"Keycloak token exchange returned no access_token: {payload}")
        return token

    async def _exchange_for_azure_token(self, kc_assertion: str) -> BearerToken:
        az = self._s.azure
        endpoint = f"{str(az.authority).rstrip('/')}/{az.tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": az.client_id,
            "scope": az.resource_scope,
            "client_assertion_type": (
                "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            ),
            "client_assertion": kc_assertion,
        }
        resp = await self._retrying_post(endpoint, data)
        payload = resp.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= 0:
            raise RuntimeError(f"Azure AD token response missing fields: {payload}")
        return BearerToken(value=token, expires_at=time.time() + expires_in)

    async def _retrying_post(self, url: str, data: dict[str, str]) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._s.retry_attempts + 1),
            wait=wait_exponential_jitter(initial=0.5, max=8.0),
            retry=retry_if_exception(_is_retryable_auth),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.post(
                    url,
                    data=data,
                    headers={"Accept": "application/json"},
                    timeout=self._s.request_timeout_s,
                )
                if resp.status_code >= 500:
                    resp.raise_for_status()
                if resp.status_code >= 400:
                    # Definitive auth rejection: bad assertion, missing scope,
                    # disabled principal. Retrying won't help.
                    raise _AuthRejection(
                        f"Auth failure {resp.status_code} at {url}: {resp.text[:512]}"
                    )
                return resp
        raise RuntimeError("unreachable")
