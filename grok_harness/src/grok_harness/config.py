from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator, model_validator


AuthMode = Literal[
    "keycloak_federated",
    "client_secret",
    "managed_identity",
    "azure_workload_identity",
    "static_bearer",
    "api_key",
]


class KeycloakSettings(BaseModel):
    """OIDC IdP issuing the federation JWT.

    Only required when auth_mode=keycloak_federated (the IL5 GCP path).
    """

    issuer: HttpUrl
    client_id: str
    audience: str = Field(
        default="api://AzureADTokenExchange",
        description="Audience claim Azure expects on the federated JWT.",
    )
    workload_token_path: Path = Field(
        default=Path("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        description="Projected SA token used as subject for RFC 8693 token exchange.",
    )
    scope: str = "openid"
    token_exchange_endpoint: HttpUrl | None = None


class AzureSettings(BaseModel):
    """Azure AD + Grok 4.3 endpoint configuration."""

    tenant_id: str
    client_id: str
    resource_scope: str = Field(
        description="Scope requested from Azure AD, e.g. 'api://grok-prod/.default'."
    )
    authority: HttpUrl = Field(
        default=HttpUrl("https://login.microsoftonline.us"),
        description="Defaults to Azure Government cloud; use commercial only for non-IL workloads.",
    )
    endpoint: HttpUrl = Field(
        description="Grok 4.3 inference endpoint, e.g. "
        "'https://grok-43.eastus2.inference.ml.azure.us'."
    )
    deployment: str = Field(default="grok-4.3")
    api_version: str = "2024-12-01-preview"

    # --- per-auth-mode fields (optional; required by the corresponding mode) ---
    client_secret: SecretStr | None = Field(
        default=None,
        description="Service-principal client secret. auth_mode=client_secret only.",
    )
    static_bearer: SecretStr | None = Field(
        default=None,
        description="Pre-acquired Azure AD bearer (e.g. from `az account get-access-token`).",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description="API key for a commercial Azure-hosted Grok endpoint "
        "(Azure AI Foundry MaaS). auth_mode=api_key only. NOT IL5-safe.",
    )
    workload_token_path: Path | None = Field(
        default=None,
        description="Path to the AKS-projected Azure AD assertion. "
        "Defaults to /var/run/secrets/azure/tokens/azure-identity-token.",
    )
    managed_identity_client_id: str | None = Field(
        default=None,
        description="Client ID of a user-assigned managed identity. "
        "Omit for the system-assigned identity.",
    )
    imds_endpoint: str | None = Field(
        default=None,
        description="Override the IMDS endpoint (e.g. for App Service which uses IDENTITY_ENDPOINT).",
    )


class HarnessSettings(BaseModel):
    auth_mode: AuthMode = Field(default="keycloak_federated")
    azure: AzureSettings
    keycloak: KeycloakSettings | None = None

    # Execution
    max_concurrency: int = Field(default=4, ge=1, le=64)
    request_timeout_s: float = Field(default=60.0, gt=0)
    retry_attempts: int = Field(default=3, ge=0, le=10)

    # TLS / FIPS
    ca_bundle: Path | None = Field(
        default=None,
        description="Agency PKI root bundle. Required in IL5; falls back to system store if unset.",
    )
    enforce_tls13: bool = True
    enforce_fips: bool = Field(
        default=True,
        description="Refuse to start if the OpenSSL backend is not FIPS-validated.",
    )

    # Audit
    audit_log_path: Path = Field(default=Path("/var/log/grok-harness/audit.jsonl"))
    redact_prompts_in_audit: bool = Field(
        default=True,
        description="Hash prompts/responses in audit logs; full content stays in the result file "
        "which lives behind the same access controls.",
    )

    @field_validator("ca_bundle")
    @classmethod
    def _ca_exists(cls, v: Path | None) -> Path | None:
        if v is not None and not v.exists():
            raise ValueError(f"ca_bundle {v} does not exist")
        return v

    @model_validator(mode="after")
    def _validate_auth_mode_fields(self) -> "HarnessSettings":
        mode = self.auth_mode
        if mode == "keycloak_federated" and self.keycloak is None:
            raise ValueError(
                "auth_mode=keycloak_federated requires keycloak settings "
                "(GH_KEYCLOAK_ISSUER, GH_KEYCLOAK_CLIENT_ID)"
            )
        if mode == "client_secret" and self.azure.client_secret is None:
            raise ValueError(
                "auth_mode=client_secret requires GH_AZURE_CLIENT_SECRET"
            )
        if mode == "static_bearer" and self.azure.static_bearer is None:
            raise ValueError(
                "auth_mode=static_bearer requires GH_AZURE_STATIC_BEARER"
            )
        if mode == "api_key" and self.azure.api_key is None:
            raise ValueError("auth_mode=api_key requires GH_AZURE_API_KEY")
        return self

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=str(self.ca_bundle) if self.ca_bundle else None)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        if self.enforce_tls13:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        return ctx


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing required env var {name}")
    return v


def _keycloak_from_env() -> KeycloakSettings | None:
    if not os.environ.get("GH_KEYCLOAK_ISSUER"):
        return None
    return KeycloakSettings(
        issuer=_req("GH_KEYCLOAK_ISSUER"),
        client_id=_req("GH_KEYCLOAK_CLIENT_ID"),
        audience=os.environ.get("GH_KEYCLOAK_AUDIENCE", "api://AzureADTokenExchange"),
        workload_token_path=Path(
            os.environ.get(
                "GH_WORKLOAD_TOKEN_PATH",
                "/var/run/secrets/kubernetes.io/serviceaccount/token",
            )
        ),
        token_exchange_endpoint=os.environ.get("GH_KEYCLOAK_TOKEN_EXCHANGE_URL") or None,
    )


def load_from_env() -> HarnessSettings:
    """Build settings from environment variables.

    auth_mode selects which fields are required:

      keycloak_federated       Keycloak issuer + client_id; SA token at the
                               workload-token path.
      client_secret            GH_AZURE_CLIENT_SECRET.
      managed_identity         No extra fields; optionally
                               GH_AZURE_MANAGED_IDENTITY_CLIENT_ID for a
                               user-assigned identity.
      azure_workload_identity  GH_AZURE_WORKLOAD_TOKEN_PATH (defaults to
                               /var/run/secrets/azure/tokens/azure-identity-token).
      static_bearer            GH_AZURE_STATIC_BEARER.
    """
    azure = AzureSettings(
        tenant_id=_req("GH_AZURE_TENANT_ID"),
        client_id=_req("GH_AZURE_CLIENT_ID"),
        resource_scope=_req("GH_AZURE_RESOURCE_SCOPE"),
        authority=os.environ.get("GH_AZURE_AUTHORITY", "https://login.microsoftonline.us"),
        endpoint=_req("GH_GROK_ENDPOINT"),
        deployment=os.environ.get("GH_GROK_DEPLOYMENT", "grok-4.3"),
        api_version=os.environ.get("GH_GROK_API_VERSION", "2024-12-01-preview"),
        client_secret=(
            SecretStr(os.environ["GH_AZURE_CLIENT_SECRET"])
            if os.environ.get("GH_AZURE_CLIENT_SECRET")
            else None
        ),
        static_bearer=(
            SecretStr(os.environ["GH_AZURE_STATIC_BEARER"])
            if os.environ.get("GH_AZURE_STATIC_BEARER")
            else None
        ),
        api_key=(
            SecretStr(os.environ["GH_AZURE_API_KEY"])
            if os.environ.get("GH_AZURE_API_KEY")
            else None
        ),
        workload_token_path=(
            Path(os.environ["GH_AZURE_WORKLOAD_TOKEN_PATH"])
            if os.environ.get("GH_AZURE_WORKLOAD_TOKEN_PATH")
            else None
        ),
        managed_identity_client_id=os.environ.get("GH_AZURE_MANAGED_IDENTITY_CLIENT_ID") or None,
        imds_endpoint=os.environ.get("GH_AZURE_IMDS_ENDPOINT") or None,
    )

    return HarnessSettings(
        auth_mode=os.environ.get("GH_AUTH_MODE", "keycloak_federated"),  # type: ignore[arg-type]
        azure=azure,
        keycloak=_keycloak_from_env(),
        ca_bundle=Path(os.environ["GH_CA_BUNDLE"]) if os.environ.get("GH_CA_BUNDLE") else None,
        enforce_tls13=os.environ.get("GH_ENFORCE_TLS13", "true").lower() == "true",
        enforce_fips=os.environ.get("GH_ENFORCE_FIPS", "true").lower() == "true",
        audit_log_path=Path(
            os.environ.get("GH_AUDIT_LOG_PATH", "/var/log/grok-harness/audit.jsonl")
        ),
        max_concurrency=int(os.environ.get("GH_MAX_CONCURRENCY", "4")),
    )
