# Test hierarchy

Tests are organized in three tiers that descend from the core of model
prompt testing outward to the supporting infrastructure. Pytest
collects them in this order; each tier is also reachable via marker
(`pytest -m core`, `-m io`, `-m infra`).

## Tier 1 — `tier1_core/` — the prompt-testing loop itself

These are the tests that, if they regress, the harness is no longer
doing its job. Everything else exists to support this loop.

| file                  | what it covers                                                |
| --------------------- | ------------------------------------------------------------- |
| `test_evaluators.py`  | the assertion logic — output matching, JSON schema, refusal, SLO budgets |
| `test_client.py`      | prompt -> Grok 4.3 -> parsed response; retries on transients; auth errors are definitive |
| `test_runner.py`      | suite execution, pass/fail/error tallies, audit emission, bounded concurrency |

Within each file, tests descend from the most representative scenario
(e.g. `test_happy_path_returns_completion`) to edge cases.

## Tier 2 — `tier2_io/` — authoring suites and consuming results

The harness has to read suites from YAML and emit results CI/operators
can act on. Important, but downstream of the core loop.

| file                | what it covers                                       |
| ------------------- | ---------------------------------------------------- |
| `test_loader.py`    | YAML -> `TestSuite`, schema validation, bad input rejected |
| `test_reporter.py`  | console summary, JUnit XML (`<failure>` vs `<error>`), JSON serialization |
| `test_cli.py`       | end-to-end via `click.testing.CliRunner`             |

## Tier 3 — `tier3_infra/` — required to operate, not what's under test

Identity federation, transport hardening, audit logging. The harness
will not start without these working, but they are not what model
prompt testing is about.

| file              | what it covers                                                  |
| ----------------- | --------------------------------------------------------------- |
| `test_auth.py`    | RFC 8693 token exchange (Keycloak) + Azure AD jwt-bearer assertion; caching, refresh, 4xx vs 5xx |
| `test_config.py`  | env loading, TLS 1.3 enforcement, agency CA bundle              |
| `test_audit.py`   | append-only JSONL, SHA-256 redaction, ISO UTC timestamps        |

## Running subsets

```bash
pytest -m core            # just the prompt-testing loop
pytest -m "core or io"    # core + suite I/O
pytest -m infra           # only the infrastructure plumbing
pytest tests/tier1_core   # equivalent path-based selection
```

## Shared fixtures

`tests/conftest.py` provides:

- `settings` — a `HarnessSettings` populated for the example Azure Gov
  endpoint and Keycloak realm, with a writable workload-token path.
- `workload_token` — projected K8s SA token (JWT-shaped, opaque to us).
- `keycloak_token_url`, `azure_token_url`, `grok_completions_url` — the
  three URLs the harness talks to, exactly as constructed by the code
  under test.
