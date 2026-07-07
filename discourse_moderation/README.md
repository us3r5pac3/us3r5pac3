# discourse-moderation

Policy-as-code moderation automation for a **CAC-gated DoD internal Discourse
forum**. It encodes the rules in [the policy spec](../) — every rule has an ID,
tier, detector, automated action, human routing, SLA, and legal authority — and
executes them with the legal guardrails the spec requires: it never suppresses
protected speech, never hard-deletes, always captures an immutable record before
acting, and only *routes* (never adjudicates) personnel matters.

The whole system runs offline: the default Discourse client is a dry run and the
conduct/protected classifier is a conservative stub, so you can evaluate posts
and read the resulting decisions and audit records without a live forum or a
model backend.

---

## How it works

```
Post ──▶ ModerationEngine.evaluate ──▶ Decision ──▶ ActionExecutor.apply
         (pure: detect → resolve             │        1. WORM snapshot (before any action)
          precedence → thresholds)           │        2. reversible Discourse action
                                             │        3. append-only audit record
                                             ▼
                                   poster notice + role referrals + SLA timers
```

* **`engine.py`** is pure: post in, `Decision` out, no side effects. All the
  legally load-bearing logic (precedence, the protected-speech guardrail,
  confidence thresholds) lives here and is unit-tested in isolation.
* **`actions.py`** performs the effects and structurally enforces
  capture-before-action, reversibility, and the absence of any delete path.

### Precedence (spec §2)

1. **T0 security** supersedes everything — quarantine/redact, open an incident,
   consume no strike.
2. **Protected-speech guardrail** — a post matching *both* a conduct (T1) rule
   and a protected (P) rule resolves to **P: route only, never hide**. False
   suppression of protected speech is the higher-cost error.
3. **T1 conduct** — auto-hide (or flag/label per rule) + mandatory human confirm.
4. **T2 usage** — soft auto action, no human.

Below a rule's flag threshold, nothing happens (spec AUT-002).

---

## Usage

```bash
pip install -e '.[dev]'

# Pure decision, no side effects:
discourse-mod evaluate tests/fixtures/spillage.json

# Snapshot + act (dry-run client) + audit:
discourse-mod evaluate tests/fixtures/spillage.json --apply

# From a Discourse webhook payload:
discourse-mod process-webhook payload.json --apply

# Human confirmation of a T1 auto-hide (strike) or reversal (restore):
discourse-mod confirm 42 --decision confirm --actor-edipi 1234567890 --role moderator

# Auto-restore T1 hides not confirmed within the TTL (spec AUT-003):
discourse-mod expire-t1

# Appeals (spec §4): a reversal restores content, expunges the strike,
# and emits a classifier-feedback event:
discourse-mod appeal 42 --appellant 1234567890
discourse-mod appeal-decide 42 --reviewer 9 --original-actor 1 --outcome reverse
```

Add `--live` to act against a real Discourse instance, configured via
`DISCOURSE_BASE_URL`, `DISCOURSE_API_KEY`, and `DISCOURSE_API_USERNAME`.

State (applied decisions, strike ledgers, appeals) is kept under `--state-dir`
(default `./.modstate`); the immutable WORM snapshots and append-only audit log
live there too and are meant to be forwarded to real WORM storage out of band.

---

## Policy-as-code

All behavior is data. [`policy/rules.yaml`](policy/rules.yaml) is the single
source of truth (change-controlled per spec AUT-006 — reviewers approve YAML,
not Python), and [`policy/environment.yaml`](policy/environment.yaml) holds the
per-deployment knobs from spec §10 (`data_ceiling`, `strike_decay_days`,
`t1_confirm_ttl_hours`, rate limits, model backend).

Adding detection for a rule is a YAML edit plus, for a new technique, one
function in `detectors.py`. The ML-dependent conduct/protected rules route
through the `Classifier` seam (`detectors.py`); drop in a platform-hosted,
in-boundary model (spec AUT-004) without touching the engine.

---

## Spec → code traceability

| Spec | Where |
| --- | --- |
| §2 tiers T0/T1/T2/P + automation authority | `models.Tier`, `engine.ModerationEngine` |
| §2 protected-speech guardrail (PRO beats CON) | `engine._decide_protected`, `test_engine_precedence.test_protected_beats_conduct_guardrail` |
| SEC-001..005 detection | `detectors.detect_classification/cui/secrets/pii/opsec` |
| CON-001..008 (incl. CON-007 flag-only, CON-008 label) | `policy/rules.yaml`, `engine._decide_t1` |
| USE-001..006 soft auto | `detectors.detect_flood/necro/attachment/template`, `engine._decide_t2` |
| PRO-001..004 route-only, never remove | `policy/rules.yaml`, `engine._decide_protected` |
| §3 strike ladder + decay + T0-bypass | `enforcement.py`, `test_enforcement.py` |
| §4 appeals (different reviewer, reversal effects) | `appeals.py`, `test_appeals.py` |
| §5.1 capture-before-action (WORM) | `records.WormStore`, `actions.ActionExecutor.apply` |
| §5.2 no hard delete | `discourse.py` (no delete method), `test_records_and_actions.test_no_hard_delete_method` |
| §5.3 append-only audit schema | `models.AuditRecord`, `records.AuditLog` |
| §8 SLA timers | `engine._deadlines`, `models.SLADeadline` |
| AUT-001 reversibility (T0 ISSM-only) | `actions.ActionExecutor.reverse`, `_REVERSAL_ROLES` |
| AUT-002 confidence thresholds | `engine._scan` + per-tier `_decide_*` |
| AUT-003 T1 human-in-loop TTL auto-restore | `__main__.expire_t1` |
| AUT-004 model governance (in-boundary + version logged) | `detectors.Classifier`, `AuditRecord.actor` |
| §10 config surface | `config.Environment`, `policy/environment.yaml` |

---

## Layout

```
discourse_moderation/
├── policy/
│   ├── rules.yaml            # all 24 rules as policy-as-code
│   └── environment.yaml      # per-deployment config surface (spec §10)
├── src/discourse_moderation/
│   ├── models.py             # pydantic domain types (Post, Rule, Decision, AuditRecord, ...)
│   ├── policy.py             # load + validate the policy
│   ├── detectors.py          # deterministic detectors + Classifier seam + stub
│   ├── engine.py             # pure decision engine (precedence + guardrail + thresholds)
│   ├── discourse.py          # DiscourseClient protocol + DryRunClient + HttpDiscourseClient
│   ├── actions.py            # reversible executor; capture-before-action; no delete
│   ├── records.py            # WORM snapshot store + append-only audit log
│   ├── enforcement.py        # strike ladder + decay + referrals
│   ├── appeals.py            # appeal intake + reversal effects
│   ├── state.py              # operational stores (applied decisions, ledgers)
│   ├── config.py             # environment config surface
│   └── __main__.py           # discourse-mod CLI
└── tests/                    # engine precedence, detectors, records/actions, enforcement, appeals, CLI
```

## Tests

```bash
pytest        # 38 tests, fully offline
ruff check .
```
