"""Records invariants (capture-before-action, append-only, no hard delete) and
action reversibility / authorization (spec §5, AUT-001)."""

from __future__ import annotations

import pytest
from conftest import make_post

from discourse_moderation import ActionKind, ModerationEngine, Tier, load_policy
from discourse_moderation.actions import NotAuthorized
from discourse_moderation.records import WormStore


def test_snapshot_written_before_action(executor):
    ex, client, worm, audit = executor
    engine = ModerationEngine(load_policy())
    post = make_post(raw="secret S//NOFORN briefing")
    decision = engine.evaluate(post)
    ex.apply(post, decision)
    # WORM snapshot exists and captured the original content.
    assert worm.has(post.id)
    assert worm.get(post.id).content == post.raw
    # Action was recorded on the client.
    assert any(c.op == "quarantine_post" for c in client.calls)


def test_snapshot_is_immutable(tmp_path):
    worm = WormStore(tmp_path / "worm")
    post = make_post(raw="original")
    worm.capture(post)
    # Second capture with different content must NOT overwrite.
    worm.capture(make_post(raw="tampered"))
    assert worm.get(post.id).content == "original"


def test_audit_is_append_only(executor):
    ex, client, worm, audit = executor
    engine = ModerationEngine(load_policy())
    for pid in (1, 2, 3):
        post = make_post(id=pid, topic_id=pid, raw="ssn 123-45-6789")
        ex.apply(post, engine.evaluate(post))
    assert len(audit.records()) == 3


def test_no_hard_delete_method():
    from discourse_moderation.discourse import DryRunClient, HttpDiscourseClient

    for cls in (DryRunClient, HttpDiscourseClient):
        assert not any("delete" in name.lower() for name in dir(cls))


def test_quarantine_reversible_by_issm_only(executor):
    ex, client, worm, audit = executor
    engine = ModerationEngine(load_policy())
    post = make_post(raw="S//NOFORN")
    decision = engine.evaluate(post)
    ex.apply(post, decision)
    assert decision.reversible_by == "issm"
    with pytest.raises(NotAuthorized):
        ex.reverse(post, decision, actor_edipi="9", actor_role="moderator")
    # ISSM can release it.
    ex.reverse(post, decision, actor_edipi="9", actor_role="issm")
    assert any(c.op == "release_quarantine" for c in client.calls)


def test_reverse_restores_original_on_redact(executor):
    ex, client, worm, audit = executor
    from discourse_moderation import Decision

    post = make_post(raw="my token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    decision = Decision(
        post_id=post.id,
        tier=Tier.T0,
        action=ActionKind.REDACT,
        matched_rule_id="SEC-003",
        reversible_by="issm",
    )
    ex.apply(post, decision)
    redact_call = next(c for c in client.calls if c.op == "redact_post")
    assert "[REDACTED]" in redact_call.args["redacted_body"]
    ex.reverse(post, decision, actor_edipi="9", actor_role="issm")
    restore_call = [c for c in client.calls if c.op == "redact_post"][-1]
    assert restore_call.args["redacted_body"] == post.raw
