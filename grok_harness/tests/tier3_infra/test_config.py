"""Tier 3 (infra) — HarnessSettings, env loading, and TLS context.

Bootstrap concerns. Ordered:

  1. Env loading       defaults, overrides, missing-required errors
  2. Transport         TLS 1.3 enforcement, no-downgrade, agency CA bundle
  3. Validation        CA bundle path must exist
"""
from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from grok_harness.config import HarnessSettings, load_from_env

pytestmark = pytest.mark.infra


_BASE_ENV = {
    "GH_KEYCLOAK_ISSUER": "https://kc.example.gov/realms/il5",
    "GH_KEYCLOAK_CLIENT_ID": "grok-harness",
    "GH_AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "GH_AZURE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
    "GH_AZURE_RESOURCE_SCOPE": "api://grok-prod/.default",
    "GH_GROK_ENDPOINT": "https://grok-43.eastus2.inference.ml.azure.us",
}


def _apply_env(monkeypatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_load_from_env_minimum(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    _apply_env(monkeypatch, _BASE_ENV)
    s = load_from_env()
    assert s.azure.tenant_id == "00000000-0000-0000-0000-000000000000"
    assert s.azure.deployment == "grok-4.3"  # default
    assert s.azure.api_version == "2024-12-01-preview"
    # Default Azure authority is the Government cloud, not commercial.
    assert "microsoftonline.us" in str(s.azure.authority)
    assert s.keycloak.audience == "api://AzureADTokenExchange"
    assert s.max_concurrency == 4
    assert s.enforce_fips is True
    assert s.enforce_tls13 is True
    assert s.redact_prompts_in_audit is True


def test_load_from_env_missing_required(monkeypatch):
    # Only set some -- omit GH_AZURE_TENANT_ID.
    for k, v in _BASE_ENV.items():
        if k != "GH_AZURE_TENANT_ID":
            monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="GH_AZURE_TENANT_ID"):
        load_from_env()


def test_load_from_env_overrides(monkeypatch, tmp_path):
    _apply_env(
        monkeypatch,
        {
            **_BASE_ENV,
            "GH_GROK_DEPLOYMENT": "grok-4.3-instruct",
            "GH_GROK_API_VERSION": "2025-01-01-preview",
            "GH_MAX_CONCURRENCY": "16",
            "GH_ENFORCE_FIPS": "false",
            "GH_ENFORCE_TLS13": "false",
            "GH_AUDIT_LOG_PATH": str(tmp_path / "a.jsonl"),
        },
    )
    s = load_from_env()
    assert s.azure.deployment == "grok-4.3-instruct"
    assert s.azure.api_version == "2025-01-01-preview"
    assert s.max_concurrency == 16
    assert s.enforce_fips is False
    assert s.enforce_tls13 is False


def test_ca_bundle_missing_file_rejected(tmp_path):
    bogus = tmp_path / "nope.pem"
    with pytest.raises(ValueError, match="does not exist"):
        HarnessSettings.model_validate(
            {
                "keycloak": {
                    "issuer": "https://kc/",
                    "client_id": "c",
                    "audience": "a",
                    "workload_token_path": str(tmp_path / "tok"),
                },
                "azure": {
                    "tenant_id": "t",
                    "client_id": "c",
                    "resource_scope": "s",
                    "authority": "https://login.microsoftonline.us",
                    "endpoint": "https://e/",
                },
                "ca_bundle": str(bogus),
            }
        )


def test_ssl_context_enforces_tls13(settings):
    settings.enforce_tls13 = True
    ctx = settings.ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_3


def test_ssl_context_does_not_downgrade_when_disabled(settings):
    settings.enforce_tls13 = False
    ctx = settings.ssl_context()
    # When TLS 1.3 is not explicitly required we still want hostname checks
    # and verified certs -- we just don't pin the minimum.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ssl_context_uses_provided_ca_bundle(tmp_path: Path, settings):
    """Wire up an agency PKI bundle and confirm the SSL context loads it."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "agency-test-ca")]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    settings.ca_bundle = ca
    ctx = settings.ssl_context()
    # If load_verify_locations succeeded, get_ca_certs() returns our cert.
    assert any(
        any(cn[0] == "commonName" and cn[1] == "agency-test-ca" for cn in entry.get("subject", [()])[0])
        if entry.get("subject")
        else False
        for entry in ctx.get_ca_certs()
    )
