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

## Tier 4 — `live/` — opt-in, hits real services

A small smoke suite that exercises the real Keycloak / Azure AD / Grok
4.3 endpoints described by whatever auth mode is configured in the
environment. Skipped by default; opt in via `GH_LIVE=1`.

| file              | what it covers                                            |
| ----------------- | --------------------------------------------------------- |
| `test_smoke.py`   | one auth round-trip + one prompt round-trip per run       |

These don't check correctness — they confirm wire-shape *acceptance* by
the real services, which mocks can't prove. Run after a federation or
deploy change as a one-shot acceptance test:

```bash
# IL5 production (auth env from your real deployment)
GH_LIVE=1 pytest tests/live -v

# Grok team validation against a non-prod Azure deployment
GH_AUTH_MODE=client_secret \
  GH_AZURE_CLIENT_SECRET=$(read-from-kv) \
  ... \
  GH_LIVE=1 pytest tests/live -v
```

## Running subsets

```bash
pytest -m core            # just the prompt-testing loop
pytest -m "core or io"    # core + suite I/O
pytest -m infra           # only the infrastructure plumbing
pytest -m live            # only live smoke (also needs GH_LIVE=1)
pytest tests/tier1_core   # equivalent path-based selection
pytest                    # all non-live tiers (live is skipped without GH_LIVE)
```

## Shared fixtures

`tests/conftest.py` provides:

- `settings` — a `HarnessSettings` populated for the example Azure Gov
  endpoint and Keycloak realm, with a writable workload-token path.
- `workload_token` — projected K8s SA token (JWT-shaped, opaque to us).
- `keycloak_token_url`, `azure_token_url`, `grok_completions_url` — the
  three URLs the harness talks to, exactly as constructed by the code
  under test.
