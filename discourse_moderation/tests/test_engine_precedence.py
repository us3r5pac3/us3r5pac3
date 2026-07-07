"""The legally load-bearing logic: tier precedence, the protected-speech
guardrail, and confidence thresholds (spec §2, AUT-002)."""

from __future__ import annotations

from conftest import FakeClassifier, make_post

from discourse_moderation import ActionKind, ModerationEngine, Tier


def test_t0_supersedes_conduct(policy):
    # A post that is both a classification spillage (T0) and scores high on a
    # conduct rule must resolve to T0.
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.99}))
    post = make_post(raw="you idiot, and by the way this is S//NOFORN")
    d = engine.evaluate(post)
    assert d.tier == Tier.T0
    assert d.action == ActionKind.QUARANTINE
    assert d.opens_incident and d.strike_delta == 0


def test_protected_beats_conduct_guardrail(policy):
    # Scores positive on BOTH a conduct rule and a protected rule -> resolves to
    # protected: route only, never auto-hide (spec §2 guardrail).
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.99, "PRO-001": 0.7}))
    d = engine.evaluate(make_post(raw="reporting fraud; also some heated language"))
    assert d.tier == Tier.P
    assert d.action == ActionKind.ROUTE_ONLY
    assert not d.requires_human_confirm


def test_protected_alone_is_route_only(policy):
    engine = ModerationEngine(policy, FakeClassifier({"PRO-003": 0.8}))
    d = engine.evaluate(make_post(raw="leadership's new policy is misguided"))
    assert d.tier == Tier.P and d.action == ActionKind.ROUTE_ONLY


def test_t1_auto_hide_requires_confirm(policy):
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.9}))
    d = engine.evaluate(make_post(raw="harassing content"))
    assert d.tier == Tier.T1
    assert d.action == ActionKind.HIDE
    assert d.requires_human_confirm
    assert d.strike_delta == 0  # strike applied by a human on confirmation


def test_t1_below_auto_threshold_flags_only(policy):
    # Between flag (0.6) and auto (0.85) thresholds -> flag, no content action.
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.7}))
    d = engine.evaluate(make_post(raw="borderline content"))
    assert d.tier == Tier.T1
    assert d.action == ActionKind.FLAG_ONLY
    assert not d.requires_human_confirm


def test_below_flag_threshold_no_action(policy):
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.3}))
    d = engine.evaluate(make_post(raw="mild"))
    assert d.action == ActionKind.NONE
    assert d.tier is None


def test_con007_flag_only_never_hides(policy):
    # Military contemptuous speech: flag only even at max confidence (command
    # adjudicates, not the forum team).
    engine = ModerationEngine(policy, FakeClassifier({"CON-007": 1.0}))
    d = engine.evaluate(make_post(affiliation="MIL", raw="the general is a clown"))
    assert d.matched_rule_id == "CON-007"
    assert d.action == ActionKind.FLAG_ONLY


def test_con008_labels_unverified(policy):
    engine = ModerationEngine(policy, FakeClassifier({"CON-008": 0.95}))
    d = engine.evaluate(make_post(raw="Official PMO guidance: everyone must..."))
    assert d.action == ActionKind.LABEL_UNVERIFIED


def test_con002_escalates_to_security(policy):
    engine = ModerationEngine(policy, FakeClassifier({"CON-002": 0.95}))
    d = engine.evaluate(make_post(raw="threat text"))
    assert d.action == ActionKind.HIDE
    assert any(r.role == "security" for r in d.referrals)


def test_t2_soft_auto_no_human(policy):
    engine = ModerationEngine(policy)
    d = engine.evaluate(make_post(topic_archived=True))  # USE-004 necro
    assert d.tier == Tier.T2
    assert d.action == ActionKind.MOVE
    assert not d.requires_human_confirm


def test_t1_carries_confirm_and_ttl_deadlines(policy):
    engine = ModerationEngine(policy, FakeClassifier({"CON-001": 0.9}))
    d = engine.evaluate(make_post(raw="x"))
    kinds = {sla.kind for sla in d.sla_deadlines}
    assert {"t1_confirm", "t1_confirm_ttl"} <= kinds
