"""Shared fixtures: harness settings and a writable workload-token path."""
from __future__ import annotations

from pathlib import Path

import pytest

from grok_harness.config import AzureSettings, HarnessSettings, KeycloakSettings


@pytest.fixture
def workload_token(tmp_path: Path) -> Path:
    p = tmp_path / "sa-token"
    p.write_text(
        # Realistic SA-token shape (header.payload.sig); the harness does not
        # validate the signature — it forwards it to Keycloak verbatim.
        "eyJhbGciOiJSUzI1NiJ9."
        "eyJzdWIiOiJzeXN0ZW06c2VydmljZWFjY291bnQ6Z3Jvay1oYXJuZXNzOnJ1bm5lciJ9."
        "stub-signature"
    )
    return p


@pytest.fixture
def settings(tmp_path: Path, workload_token: Path) -> HarnessSettings:
    return HarnessSettings(
        keycloak=KeycloakSettings(
            issuer="https://kc.example.gov/realms/il5",
            client_id="grok-harness",
            audience="api://AzureADTokenExchange",
            workload_token_path=workload_token,
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
        retry_attempts=2,
    )


@pytest.fixture
def keycloak_token_url() -> str:
    return "https://kc.example.gov/realms/il5/protocol/openid-connect/token"


@pytest.fixture
def azure_token_url() -> str:
    return (
        "https://login.microsoftonline.us/"
        "00000000-0000-0000-0000-000000000000/oauth2/v2.0/token"
    )


@pytest.fixture
def grok_completions_url() -> str:
    return (
        "https://grok-43.eastus2.inference.ml.azure.us"
        "/openai/deployments/grok-4.3/chat/completions"
        "?api-version=2024-12-01-preview"
    )
