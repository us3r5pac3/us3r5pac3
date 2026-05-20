"""CLI integration: run a suite end-to-end via click.testing.CliRunner.

The federation provider and Grok HTTP calls are stubbed via respx + a
monkeypatched token getter, so this test exercises the real wiring
between config, loader, runner, reporter, and audit pipelines.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from grok_harness.__main__ import cli


@pytest.fixture
def isolated_env(monkeypatch, tmp_path: Path):
    sa = tmp_path / "sa-token"
    sa.write_text("stub.sa.token")
    monkeypatch.setenv("GH_KEYCLOAK_ISSUER", "https://kc.example.gov/realms/il5")
    monkeypatch.setenv("GH_KEYCLOAK_CLIENT_ID", "grok-harness")
    monkeypatch.setenv("GH_WORKLOAD_TOKEN_PATH", str(sa))
    monkeypatch.setenv("GH_AZURE_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("GH_AZURE_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("GH_AZURE_RESOURCE_SCOPE", "api://grok-prod/.default")
    monkeypatch.setenv("GH_GROK_ENDPOINT", "https://grok-43.eastus2.inference.ml.azure.us")
    monkeypatch.setenv("GH_GROK_DEPLOYMENT", "grok-4.3")
    monkeypatch.setenv("GH_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("GH_ENFORCE_FIPS", "false")
    monkeypatch.setenv("GH_ENFORCE_TLS13", "false")
    return tmp_path


@pytest.fixture
def stub_tokens(monkeypatch):
    from grok_harness import auth

    async def _fake(self):
        return auth.BearerToken(value="stub", expires_at=time.time() + 600)

    monkeypatch.setattr(auth.FederatedTokenProvider, "get_token", _fake)


def _suite_yaml() -> str:
    return (
        "name: cli-smoke\n"
        "cases:\n"
        "  - id: c1\n"
        "    messages:\n"
        "      - role: user\n"
        "        content: hi\n"
        "    assertions:\n"
        "      - kind: contains\n"
        "        value: ok\n"
    )


def test_cli_help():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Grok 4.3 prompt testing harness" in result.output


def test_cli_run_success_writes_outputs(isolated_env: Path, stub_tokens, monkeypatch):
    suite_path = isolated_env / "suite.yaml"
    suite_path.write_text(_suite_yaml())
    json_out = isolated_env / "results.json"
    junit_out = isolated_env / "results.xml"

    url = (
        "https://grok-43.eastus2.inference.ml.azure.us"
        "/openai/deployments/grok-4.3/chat/completions"
        "?api-version=2024-12-01-preview"
    )

    with respx.mock() as router:
        router.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            cli,
            [
                "run",
                str(suite_path),
                "--json-out",
                str(json_out),
                "--junit-out",
                str(junit_out),
            ],
        )

    assert result.exit_code == 0, result.output
    assert "1/1 passed" in result.output

    data = json.loads(json_out.read_text())
    assert data[0]["case_id"] == "c1"
    assert data[0]["passed"] is True

    suite = ET.parse(junit_out).getroot().find("testsuite")
    assert suite.get("tests") == "1"
    assert suite.get("failures") == "0"
    assert suite.get("errors") == "0"

    audit = (isolated_env / "audit.jsonl").read_text().strip().splitlines()
    assert len(audit) >= 4  # suite.start + case.start + case.end + suite.end


def test_cli_run_nonzero_exit_on_failure(isolated_env: Path, stub_tokens):
    suite_path = isolated_env / "suite.yaml"
    suite_path.write_text(
        "name: cli-fail\n"
        "cases:\n"
        "  - id: c1\n"
        "    messages:\n"
        "      - role: user\n"
        "        content: hi\n"
        "    assertions:\n"
        "      - kind: contains\n"
        "        value: berlin\n"
    )

    url = (
        "https://grok-43.eastus2.inference.ml.azure.us"
        "/openai/deployments/grok-4.3/chat/completions"
        "?api-version=2024-12-01-preview"
    )
    with respx.mock() as router:
        router.post(url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "paris"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(cli, ["run", str(suite_path)])

    assert result.exit_code != 0
    assert "0/1 passed" in result.output


def test_cli_run_missing_env_errors(tmp_path: Path, monkeypatch, stub_tokens):
    # Clear only Azure tenant to trigger the env error.
    for key in list(os.environ):
        if key.startswith("GH_"):
            monkeypatch.delenv(key, raising=False)
    suite_path = tmp_path / "s.yaml"
    suite_path.write_text(_suite_yaml())
    result = CliRunner().invoke(cli, ["run", str(suite_path)])
    assert result.exit_code != 0
