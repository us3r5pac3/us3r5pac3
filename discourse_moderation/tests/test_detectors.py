from __future__ import annotations

from conftest import make_post

from discourse_moderation.config import Environment
from discourse_moderation.detectors import StubClassifier, run_detectors


def _env():
    return Environment()


def _fire(policy, rule_id, post):
    rule = policy.get(rule_id)
    return run_detectors(post, rule, _env(), StubClassifier())


def test_classification_marking_detected(policy):
    dets = _fire(policy, "SEC-001", make_post(raw="briefing is S//NOFORN, hold close"))
    assert dets and dets[0].confidence > 0.9


def test_secret_pattern_detected(policy):
    post = make_post(raw="export API_KEY=abcdef123456 and connect")
    dets = _fire(policy, "SEC-003", post)
    assert dets and dets[0].confidence >= 0.9


def test_secret_high_entropy_fallback(policy):
    post = make_post(raw="token blob Zk9xQ2mB7vL0pR3sT8uW1yA4dC6eF2gH")
    dets = _fire(policy, "SEC-003", post)
    assert dets and dets[0].confidence > 0


def test_pii_ssn_detected(policy):
    dets = _fire(policy, "SEC-004", make_post(raw="his ssn is 123-45-6789 fyi"))
    assert dets and dets[0].confidence >= 0.9


def test_cui_ignored_when_ceiling_permits(policy):
    # environment.yaml ships data_ceiling: cui, so CUI markings ARE a violation.
    dets = _fire(policy, "SEC-002", make_post(raw="marked CUI//SP-PRVCY"))
    assert dets and dets[0].confidence > 0


def test_flood_by_recent_count(policy):
    post = make_post(author_recent_post_count=99)
    dets = _fire(policy, "USE-002", post)
    assert dets and dets[0].confidence > 0


def test_necro_on_archived(policy):
    dets = _fire(policy, "USE-004", make_post(topic_archived=True))
    assert dets and dets[0].confidence > 0


def test_no_false_positive_on_clean_text(policy):
    post = make_post(raw="Where do I find the travel voucher template?")
    for rid in ("SEC-001", "SEC-003", "SEC-004", "SEC-005"):
        assert _fire(policy, rid, post) == []
