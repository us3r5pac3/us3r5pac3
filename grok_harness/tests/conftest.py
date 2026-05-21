"""Shared fixtures and tier-ordered collection.

The directory layout (tier1_core / tier2_io / tier3_infra) already
gives the right macro ordering. This hook enforces *intra-tier*
ordering so that, within each tier, files run in the order that best
informs the reader from the most fundamental concern outward.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from grok_harness.config import AzureSettings, HarnessSettings, KeycloakSettings


# File-level importance ordering inside each tier. Files not listed here
# (e.g. new ones) fall to the end of their tier in alphabetical order.
_INTRA_TIER_ORDER: dict[str, int] = {
    # tier1_core: send a prompt -> judge the response -> orchestrate a suite
    # -> drive the suite under load -> measure assertion quality
    "tier1_core/test_client.py": 0,
    "tier1_core/test_evaluators.py": 1,
    "tier1_core/test_runner.py": 2,
    "tier1_core/test_load.py": 3,
    "tier1_core/test_calibrate.py": 4,
    # tier2_io: read suite -> emit results -> wire it together
    "tier2_io/test_loader.py": 0,
    "tier2_io/test_reporter.py": 1,
    "tier2_io/test_cli.py": 2,
    # tier3_infra: bootstrap config -> per-request auth -> observability
    "tier3_infra/test_config.py": 0,
    "tier3_infra/test_auth.py": 1,
    "tier3_infra/test_audit.py": 2,
}


def pytest_collection_modifyitems(config, items):
    def key(item):
        path = str(item.path)
        rel = path.split("/tests/", 1)[-1]
        # Sort by (tier directory, intra-tier ordinal, original index for stability)
        tier = rel.split("/", 1)[0]
        for suffix, ordinal in _INTRA_TIER_ORDER.items():
            if rel == suffix or rel.endswith("/" + suffix):
                return (tier, ordinal, 0)
        return (tier, 99, rel)

    items.sort(key=key)


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
