from __future__ import annotations

import os
import ssl
from pathlib import Path

from pydantic import BaseModel, Field, HttpUrl, SecretStr, field_validator


class KeycloakSettings(BaseModel):
    """OIDC IdP issuing the federation JWT.

    In an IL5 deployment Keycloak runs inside the GCP Assured Workload
    boundary and is reachable only over the agency's private interconnect.
    """

    issuer: HttpUrl
    client_id: str
    audience: str = Field(
        description="Audience claim Azure expects on the federated JWT, "
        "typically 'api://AzureADTokenExchange'."
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


class HarnessSettings(BaseModel):
    keycloak: KeycloakSettings
    azure: AzureSettings

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

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=str(self.ca_bundle) if self.ca_bundle else None)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        if self.enforce_tls13:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        return ctx


def load_from_env() -> HarnessSettings:
    """Build settings from environment variables.

    Secrets (client secrets, tokens) are never read from env directly; auth
    relies on the projected SA token and workload identity federation.
    """

    def req(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise RuntimeError(f"Missing required env var {name}")
        return v

    return HarnessSettings(
        keycloak=KeycloakSettings(
            issuer=req("GH_KEYCLOAK_ISSUER"),
            client_id=req("GH_KEYCLOAK_CLIENT_ID"),
            audience=os.environ.get("GH_KEYCLOAK_AUDIENCE", "api://AzureADTokenExchange"),
            workload_token_path=Path(
                os.environ.get(
                    "GH_WORKLOAD_TOKEN_PATH",
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                )
            ),
            token_exchange_endpoint=os.environ.get("GH_KEYCLOAK_TOKEN_EXCHANGE_URL") or None,
        ),
        azure=AzureSettings(
            tenant_id=req("GH_AZURE_TENANT_ID"),
            client_id=req("GH_AZURE_CLIENT_ID"),
            resource_scope=req("GH_AZURE_RESOURCE_SCOPE"),
            authority=os.environ.get("GH_AZURE_AUTHORITY", "https://login.microsoftonline.us"),
            endpoint=req("GH_GROK_ENDPOINT"),
            deployment=os.environ.get("GH_GROK_DEPLOYMENT", "grok-4.3"),
            api_version=os.environ.get("GH_GROK_API_VERSION", "2024-12-01-preview"),
        ),
        ca_bundle=Path(os.environ["GH_CA_BUNDLE"]) if os.environ.get("GH_CA_BUNDLE") else None,
        enforce_tls13=os.environ.get("GH_ENFORCE_TLS13", "true").lower() == "true",
        enforce_fips=os.environ.get("GH_ENFORCE_FIPS", "true").lower() == "true",
        audit_log_path=Path(
            os.environ.get("GH_AUDIT_LOG_PATH", "/var/log/grok-harness/audit.jsonl")
        ),
        max_concurrency=int(os.environ.get("GH_MAX_CONCURRENCY", "4")),
    )
