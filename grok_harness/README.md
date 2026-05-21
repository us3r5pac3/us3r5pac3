# grok-harness

Prompt testing harness for **Grok 4.3** on Azure. Authors a YAML suite of
prompts, runs them through composable assertions, and reports pass/fail
in JSON, JUnit, and console formats. Originally built for IL5 (GCP
caller, Keycloak-federated identity into Azure Government), but the auth
layer is pluggable so the Grok team can run the same harness directly
from inside Azure with a service principal, managed identity, AKS
workload identity, a pre-fetched token, or a plain API key against a
commercial deployment.

---

## Contents

1. [What you can test](#what-you-can-test) — four test approaches, one
   example file per approach.
2. [Where you can run it from](#where-you-can-run-it-from) — six auth
   modes spanning IL5 prod to local dev.
3. [End-to-end user journeys](#end-to-end-user-journeys) — concrete
   recipes combining a test approach with an auth mode.
4. [Authoring your own suite](#authoring-your-own-suite)
5. [IL5 hardening](#il5-hardening)
6. [Repository layout](#repository-layout)
7. [Architecture, tests](#architecture-and-tests)

---

## What you can test

Each test approach is demonstrated by its own focused example file in
`examples/`. Use them as templates; copy and edit the `messages` for
your prompts.

| Approach | What it answers | Assertion kinds | Starter file |
| --- | --- | --- | --- |
| **Functional output** | Does the model say the right thing? | `contains`, `regex`, `equals`, `not_contains` | [`examples/functional.yaml`](examples/functional.yaml) |
| **Structured output** | Does the model produce JSON that matches a schema? | `json_schema` | [`examples/structured.yaml`](examples/structured.yaml) |
| **Policy and safety** | Does it refuse what it should and avoid leaking CUI/PII? | `refusal`, `not_contains` | [`examples/safety.yaml`](examples/safety.yaml) |
| **SLO budgets** | Does it stay within latency and token limits? | `max_latency_ms`, `max_tokens`, `min_tokens` | [`examples/slo.yaml`](examples/slo.yaml) |
| **Combined smoke** | All of the above in one suite for a quick deploy check. | mixed | [`examples/full-suite.yaml`](examples/full-suite.yaml) |
| **Scale-up / throughput** | Where does the deployment break under concurrent load? | n/a — driven by `grok-harness load` | any suite |

### Functional output journey

You want to confirm the model returns the right token (or pattern) for a
known prompt.

```bash
grok-harness run examples/functional.yaml \
  --json-out out/functional.json \
  --junit-out out/functional.xml
```

Each case in `functional.yaml` lists `contains`, `regex`, or `equals`
assertions. Exit code is non-zero if any case fails. The console summary
shows latency per case; the JSON has the full request/response for
offline triage.

### Structured-output journey

You want the model to produce JSON your downstream consumers will parse.

```bash
grok-harness run examples/structured.yaml --json-out out/structured.json
```

`json_schema` assertions use the `jsonschema` library when available
(installed by default with `[dev]`) and fall back to a required-keys
shape check otherwise. Pair a schema assertion with a `contains` check
on the expected value to catch both shape and content regressions.

### Policy and safety journey

You want to verify the model refuses to repeat CUI markers, never echoes
SSN-shaped strings, and doesn't *over*-refuse benign requests.

```bash
grok-harness run examples/safety.yaml --junit-out out/safety.xml
```

`safety.yaml` includes three cases that exercise the most common
patterns: refusal-when-required, refusal-when-NOT-required (over-refusal
regression), and PII non-leakage.

### SLO journey

You want to catch latency or output-length regressions across deploys.

```bash
grok-harness run examples/slo.yaml --junit-out out/slo.xml
```

SLO assertions check a single shot per case. For *deployment-level* SLOs
under load (p95 across hundreds of requests, throughput per second, error
rate under saturation), use `grok-harness load` — see the next section.

### Scale-up / throughput journey

`grok-harness run` tests prompt correctness one shot at a time.
`grok-harness load` replays cases from a suite under increasing
concurrency to characterize the deployment itself — find the knee where
latency degrades or errors spike, set rate-limit budgets, and catch
regressions in scale-out.

```bash
# Step ramp: 1, 2, 4, 8, 16 concurrent workers, 10s per step,
# fail the run if any step exceeds 800ms p95 or 5% errors.
grok-harness load examples/functional.yaml \
  --profile step-ramp --max-concurrency 16 --duration 10 \
  --max-p95-latency-ms 800 --max-error-rate 0.05 \
  --json-out out/load.json
```

Output is a per-step table with concurrency, requests, successes,
errors, p50/p95/p99 latency, throughput (RPS), and any failed SLO
gates:

```
Load test: suite=functional auth_mode=client_secret steps=5 passed=True

  conc  dur(s)   reqs    ok   err    p50    p95    p99   rps  gates
  ----  ------  -----  ----  ----  -----  -----  -----  ----  -----
     1    10.0     35    35     0    240    320    350   3.5  ok
     2    10.0     65    65     0    245    330    380   6.5  ok
     4    10.0    120   118     2    260    450    600  12.0  ok
     8    10.0    220   200    20    320    700    900  22.0  ok
    16    10.0    300   240    60    450   1200   2800  30.0  FAIL: p95 latency 1200ms > 800ms
```

The JSON report has the full per-step metrics including `errors_by_status`
(429 vs 500 vs other), so you can separate throttling from server errors.

**Profiles:**

| Profile | When to use | Knobs |
| --- | --- | --- |
| `step-ramp` *(default)* | Find the knee — concurrency doubles from 1 to `--max-concurrency` | `--max-concurrency`, `--duration` |
| `sustained` | Stability test at a fixed level | `--concurrency`, `--duration` |
| `custom` | Bespoke step plan | `--steps '1:5,4:10,16:30'` |

**SLO gates** (any combination):

| Flag | Fails the step if … |
| --- | --- |
| `--max-p95-latency-ms` | p95 exceeds the threshold |
| `--max-error-rate` | error fraction (0..1) exceeds the threshold |
| `--min-throughput-rps` | sustained throughput falls below the threshold |

A non-zero exit code on any failed gate makes this drop straight into a
CI gate. Pair with the audit log forwarded to a metrics backend to
trend p95 / throughput / 429 rate over time.

---

## Where you can run it from

Auth is selected with `GH_AUTH_MODE`. Pick the one that matches your
execution environment.

| `GH_AUTH_MODE` | When to use | What you supply | IL5-safe |
| --- | --- | --- | --- |
| `keycloak_federated` *(default)* | IL5 production: GCP/GKE caller, Azure Gov backend | Keycloak issuer + client ID; projected K8s SA token | yes |
| `client_secret` | Azure team validation: simplest path inside Azure | `GH_AZURE_CLIENT_SECRET` (from Key Vault) | no |
| `managed_identity` | Azure VM, App Service, Container Instances, legacy AKS | nothing extra (system-assigned) or `GH_AZURE_MANAGED_IDENTITY_CLIENT_ID` (user-assigned) | yes (no secrets) |
| `azure_workload_identity` | Modern AKS with the `azwi` mutating webhook | `GH_AZURE_WORKLOAD_TOKEN_PATH` (defaults to `/var/run/secrets/azure/tokens/azure-identity-token`) | yes (no secrets) |
| `static_bearer` | Interactive dev with a pre-fetched Azure AD token | `GH_AZURE_STATIC_BEARER` from `az account get-access-token` | no |
| `api_key` | **Dev only**: plain key against a commercial Azure-hosted Grok endpoint | `GH_AZURE_API_KEY` | no |

### IL5 production — `keycloak_federated`

```
+---------------------------+        +-------------------+        +-------------------------+
|  GKE pod (GCP, IL5 zone)  | -----> |  Keycloak (IdP)   | -----> |  Azure AD (Gov tenant)  |
|  projected SA token (JWT) |   1    |  RFC 8693 token   |   2    |  client_credentials w/  |
+---------------------------+        |  exchange         |        |  jwt-bearer assertion   |
                                     +-------------------+        +-------------------------+
                                                                            |
                                                                            v
                                                              +-------------------------------+
                                                              |  Grok 4.3 (Azure Gov MaaS)    |
                                                              +-------------------------------+
```

1. Pod's projected SA token is sent to Keycloak as `subject_token` (RFC 8693).
2. The returned JWT is POSTed to Azure AD as `client_assertion` with
   `grant_type=client_credentials`. Azure validates against the App
   Registration's federated credential.

No long-lived secrets touch disk. Required env:

```bash
export GH_AUTH_MODE=keycloak_federated  # default; can be omitted
export GH_KEYCLOAK_ISSUER=https://keycloak.svc.agency.gov/realms/il5
export GH_KEYCLOAK_CLIENT_ID=grok-harness
export GH_AZURE_TENANT_ID=...
export GH_AZURE_CLIENT_ID=...
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://grok-43.eastus2.inference.ml.azure.us'
export GH_CA_BUNDLE=/etc/ssl/certs/agency-pki-bundle.pem
```

### Grok team validation — `client_secret`

Simplest path inside Azure. **Not IL5-safe** (secret lives in env).

```bash
export GH_AUTH_MODE=client_secret
export GH_AZURE_TENANT_ID=...
export GH_AZURE_CLIENT_ID=...                                # SP app ID
export GH_AZURE_CLIENT_SECRET=...                            # from Key Vault
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://<deployment>.inference.ml.azure.com'
export GH_ENFORCE_FIPS=false                                 # not in IL5
```

### CI in AKS — `azure_workload_identity`

For pipelines: no secret in env, the AKS webhook projects an Azure AD
assertion into the pod.

```bash
export GH_AUTH_MODE=azure_workload_identity
# GH_AZURE_WORKLOAD_TOKEN_PATH defaults to /var/run/secrets/azure/tokens/azure-identity-token
export GH_AZURE_TENANT_ID=...
export GH_AZURE_CLIENT_ID=...                                # SP that trusts the AKS issuer
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://<deployment>.inference.ml.azure.com'
```

### Azure VM / App Service — `managed_identity`

The host identity is exchanged at IMDS for an access token. No secrets.

```bash
export GH_AUTH_MODE=managed_identity
# Optional: user-assigned identity. Omit for the system-assigned one.
# export GH_AZURE_MANAGED_IDENTITY_CLIENT_ID=...
# Optional: App Service uses IDENTITY_ENDPOINT instead of the IMDS IP.
# export GH_AZURE_IMDS_ENDPOINT=$IDENTITY_ENDPOINT
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://<deployment>.inference.ml.azure.com'
```

### Interactive dev — `static_bearer`

Use a token you already have (e.g. from `az account get-access-token`).
Good for one-off runs.

```bash
export GH_AUTH_MODE=static_bearer
export GH_AZURE_STATIC_BEARER=$(az account get-access-token \
  --resource api://grok-prod --query accessToken -o tsv)
export GH_AZURE_TENANT_ID=...
export GH_AZURE_CLIENT_ID=...
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://<deployment>.inference.ml.azure.com'
```

### Local dev against commercial — `api_key`

Bypass federation entirely. The key is sent as the `api-key` header
(Azure OpenAI / Foundry convention). **Not IL5-safe.**

```bash
export GH_AUTH_MODE=api_key
export GH_AZURE_API_KEY=...                                       # deployment key
export GH_AZURE_AUTHORITY=https://login.microsoftonline.com       # commercial cloud
export GH_AZURE_TENANT_ID=placeholder                             # unused in this mode
export GH_AZURE_CLIENT_ID=placeholder
export GH_AZURE_RESOURCE_SCOPE='api://grok-prod/.default'
export GH_GROK_ENDPOINT='https://<commercial-deployment>.inference.ml.azure.com'
export GH_ENFORCE_FIPS=false
```

---

## End-to-end user journeys

Concrete recipes that combine a test approach with an auth mode.

### Journey 1 — IL5 production smoke after deploy

**Scenario:** New Grok build is rolling into the Gov region. You want a
green-light JUnit report on a mixed suite before production traffic
hits.

```bash
# Auth env from "IL5 production" above
grok-harness run examples/full-suite.yaml \
  --json-out out/results.json \
  --junit-out out/results.xml
```

Wire the JUnit output into your ATO test pipeline; the audit JSONL is
forwarded to WORM storage by Fluent Bit.

### Journey 2 — Grok team validating structured output before release

**Scenario:** You're on the Grok team running validation against your
non-prod Azure deployment. You care most about JSON-schema conformance.

```bash
# Auth env from "Grok team validation" (client_secret) above
grok-harness run examples/structured.yaml \
  --json-out out/structured-validation.json
```

Failures land in the JSON with the full request and response (latency,
token counts, finish_reason, request_id) so you can correlate with
server-side traces via `x-ms-request-id`.

### Journey 3 — Catch refusal/over-refusal regressions in CI

**Scenario:** Your AKS-hosted CI pipeline runs the safety suite on every
merge. You want it to fail the build if the model starts refusing
benign requests or starts leaking CUI markers.

```bash
# Auth env from "CI in AKS" (azure_workload_identity) above
grok-harness run examples/safety.yaml --junit-out out/safety.xml --fail-fast
```

`--fail-fast` makes the exit code equal the number of failed cases —
useful for partial-credit gates.

### Journey 4 — SLO regression check across deploys

**Scenario:** After a deployment scale-out, confirm p95 latency and
output length haven't drifted on representative traffic.

```bash
# Auth env for whichever environment you're hitting
export GH_MAX_CONCURRENCY=8
grok-harness run examples/slo.yaml --json-out out/slo.json
```

Inspect `out/slo.json` for per-case `latency_ms`. For *aggregate* p95
across hundreds of requests, see Journey 4b below.

### Journey 4b — Find the deployment's knee under load

**Scenario:** Before promoting a new model rev, characterize where it
breaks. You want a step ramp that confirms it holds an SLO at the
expected concurrency and surfaces the failure mode beyond that.

```bash
# Auth env for whichever environment you're hitting
grok-harness load examples/functional.yaml \
  --profile step-ramp --max-concurrency 32 --duration 15 \
  --max-p95-latency-ms 1000 --max-error-rate 0.02 \
  --min-throughput-rps 10 \
  --json-out out/load-ramp.json
```

The exit code is non-zero on the first step that breaches a gate, so
this drops into a CI promotion gate. Trend the per-step JSON over time
to spot drift before users do.

For stability instead of capacity, use `--profile sustained
--concurrency 8 --duration 600` (10 minutes at 8 concurrent) and watch
the p95 stay flat.

### Journey 5 — Local iteration on prompts

**Scenario:** You're on a workstation iterating on a new system prompt
before committing it. You have an API key for a commercial-cloud
non-prod Grok deployment.

```bash
# Auth env from "Local dev against commercial" (api_key) above
# Copy and edit one of the example files:
cp examples/functional.yaml my-suite.yaml
# Edit my-suite.yaml — update messages, tweak assertions
grok-harness run my-suite.yaml
```

The console summary is enough for local iteration; add `--json-out` once
you want to diff runs.

---

## Authoring your own suite

Suites are YAML with `name` and a list of `cases`. Each case has
`messages` (OpenAI chat format), generation params, tags, and a list of
`assertions`. Start from the example file that's closest to your use
case.

```yaml
- id: my-custom-check
  description: One sentence on what you're verifying.
  tags: [my-feature]
  temperature: 0.0
  seed: 42                    # optional, for reproducible cases
  max_tokens: 256
  messages:
    - role: system
      content: <your system prompt>
    - role: user
      content: <your user message>
  assertions:
    - kind: contains
      value: <expected substring>
    - kind: max_latency_ms
      value: 5000
```

All assertion kinds and their value shapes are listed in
[`src/grok_harness/evaluators.py`](src/grok_harness/evaluators.py).

---

## IL5 hardening

The harness enforces or supports each of these; the surrounding platform
owns the rest:

- **TLS 1.3 minimum** (`GH_ENFORCE_TLS13=true`, default on).
- **Agency PKI bundle** via `GH_CA_BUNDLE`; system store is the fallback.
- **FIPS 140-3** gate (`GH_ENFORCE_FIPS=true`) — refuses to start if the
  OpenSSL backend doesn't look FIPS-validated. Pair with a FIPS base
  image for a real guarantee.
- **No secret material in env** in `keycloak_federated`,
  `managed_identity`, and `azure_workload_identity` modes.
- **Azure Government authority** is the default (`login.microsoftonline.us`).
- **Audit log** is append-only JSONL with UTC ISO timestamps;
  prompts/responses are SHA-256 hashed by default. Forward to WORM
  storage out-of-band.
- **Bounded concurrency** (`GH_MAX_CONCURRENCY`) keeps the harness
  within the deployment's rate-limit budget.
- **Retries** use jittered exponential backoff on 408/429/5xx and
  transient network errors. 4xx auth rejections and `content_filter`
  responses are *not* retried.

---

## Repository layout

```
grok_harness/
├── README.md                  # this file
├── pyproject.toml
├── examples/                  # one YAML per test approach
│   ├── .env.example           # all env vars, grouped by auth mode
│   ├── functional.yaml        # contains / regex / equals
│   ├── structured.yaml        # json_schema
│   ├── safety.yaml            # refusal / not_contains (CUI, PII)
│   ├── slo.yaml               # latency / token budgets
│   └── full-suite.yaml        # combined smoke suite
├── src/grok_harness/
│   ├── __main__.py            # click CLI: `grok-harness run|load`
│   ├── config.py              # env-driven settings + TLS context
│   ├── auth.py                # six pluggable token providers + factory
│   ├── client.py              # Grok 4.3 chat-completions client
│   ├── evaluators.py          # assertion kinds (one-shot correctness)
│   ├── load.py                # step-ramp load runner + SLO gates
│   ├── loader.py              # YAML -> TestSuite
│   ├── runner.py              # async, concurrency-bounded suite execution
│   ├── reporter.py            # JSON + JUnit + console
│   ├── audit.py               # structlog JSONL audit pipeline
│   └── models.py              # pydantic types
└── tests/                     # tiered by importance to prompt testing
    ├── README.md              # tier hierarchy
    ├── conftest.py            # shared fixtures + ordering hook
    ├── tier1_core/            # the prompt-testing loop itself
    ├── tier2_io/              # authoring, reporting, CLI
    └── tier3_infra/           # auth, config, audit, transport
```

---

## Architecture and tests

For the design rationale (typed seams between modules, why the federation
is the shape it is, what's pure vs. effectful), see the architecture
walkthrough in the conversation history or the inline docstrings on
each module.

For the test layout, see [`tests/README.md`](tests/README.md). The suite
is organized into three importance tiers and can be sliced via pytest
markers:

```bash
pytest -m core            # the prompt-testing loop + load runner
pytest -m io              # suite authoring + reporting
pytest -m infra           # auth + config + audit
pytest                    # all of the above (121 tests)
```

Every external surface (Keycloak, Azure AD, IMDS, Grok) is mocked via
`respx`, so the full suite runs offline with no real network calls.
