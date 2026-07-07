"""The moderation decision engine.

`evaluate` is a **pure** function: post + policy in, `Decision` out, no side
effects. All I/O (snapshotting, Discourse calls, audit) happens later in
`actions.py` against the returned decision. This keeps the legally load-bearing
logic — precedence, the protected-speech guardrail, confidence thresholds —
testable in isolation.

Precedence (spec §2, §5):
    1. T0 security supersedes everything: quarantine/redact, open incident,
       consume no strike.
    2. Protected-speech guardrail: a post matching BOTH a conduct (T1) rule and
       a protected (P) rule resolves to P — route, never auto-hide. False
       suppression of protected speech is the higher-cost error (spec §2).
       A P match on its own is likewise route-only.
    3. T1 prohibited conduct: auto-hide (or flag/label per rule) + mandatory
       human confirmation.
    4. T2 usage: soft auto action, no human.
Below a rule's flag threshold, nothing happens (spec AUT-002).
"""

from __future__ import annotations

from datetime import timedelta

from .config import Environment
from .detectors import Classifier, StubClassifier, run_detectors
from .models import (
    ActionKind,
    Decision,
    Detection,
    Post,
    Referral,
    Rule,
    SLADeadline,
    Tier,
    utcnow,
)
from .policy import Policy

# T0 notice wording is fixed by spec §4: security holds are the only removals
# that do not disclose the specific rule to the poster.
_T0_NOTICE = "This content was held for security review."

_SLA_KIND_BY_ROLE = {
    "issm": "issm_alert",
    "cyber": "cyber_alert",
    "privacy_officer": "privacy_notify",
    "opsec_officer": "opsec_alert",
}


class ModerationEngine:
    def __init__(self, policy: Policy, classifier: Classifier | None = None):
        self.policy = policy
        self.env: Environment = policy.environment
        self.classifier = classifier or StubClassifier()

    def _best_detection(self, post: Post, rule: Rule) -> Detection | None:
        """Highest-confidence detection among a rule's detectors."""
        dets = run_detectors(post, rule, self.env, self.classifier)
        return max(dets, default=None, key=lambda d: d.confidence)

    def _scan(self, post: Post) -> dict[Tier, list[tuple[Rule, Detection]]]:
        """Run every rule's detectors; keep detections at/above flag threshold."""
        by_tier: dict[Tier, list[tuple[Rule, Detection]]] = {t: [] for t in Tier}
        for rule in self.policy.rules:
            det = self._best_detection(post, rule)
            if det is not None and det.confidence >= rule.flag_only_threshold:
                by_tier[rule.tier].append((rule, det))
        return by_tier

    def _appeal_link(self, post: Post) -> str:
        return f"/moderation/appeals/new?post_id={post.id}"

    def _notice(self, rule: Rule, post: Post) -> str:
        return f"[{rule.id}] {rule.policy_excerpt} Appeal: {self._appeal_link(post)}"

    def _deadlines(self, rule: Rule) -> list[SLADeadline]:
        now = utcnow()
        out: list[SLADeadline] = []
        if rule.sla_minutes is not None:
            if rule.tier == Tier.T0:
                kind = _SLA_KIND_BY_ROLE.get(rule.human_role, "security_alert")
                out.append(SLADeadline(kind=kind, due_at=now + timedelta(minutes=rule.sla_minutes)))
            elif rule.tier == Tier.T1:
                out.append(
                    SLADeadline(kind="t1_confirm", due_at=now + timedelta(minutes=rule.sla_minutes))
                )
                out.append(
                    SLADeadline(
                        kind="t1_confirm_ttl",
                        due_at=now + timedelta(hours=self.env.t1_confirm_ttl_hours),
                    )
                )
        return out

    def evaluate(self, post: Post) -> Decision:
        by_tier = self._scan(post)

        # 1. Tier 0 supersedes everything.
        t0 = by_tier[Tier.T0]
        if t0:
            return self._decide_t0(post, t0)

        # 2. Protected-speech guardrail. Any P match — alone OR co-triggered with
        #    a conduct rule — resolves to route-only.
        pro = by_tier[Tier.P]
        if pro:
            return self._decide_protected(post, pro, co_triggered=bool(by_tier[Tier.T1]))

        # 3. Tier 1 conduct.
        if by_tier[Tier.T1]:
            return self._decide_t1(post, by_tier[Tier.T1])

        # 4. Tier 2 usage.
        if by_tier[Tier.T2]:
            return self._decide_t2(post, by_tier[Tier.T2])

        return Decision(post_id=post.id, rationale="no rule matched at or above flag threshold")

    # ---- per-tier resolution -------------------------------------------------

    def _decide_t0(self, post: Post, matches: list[tuple[Rule, Detection]]) -> Decision:
        rule, det = max(matches, key=lambda rd: rd[1].confidence)
        auto = det.confidence >= rule.auto_action_threshold
        action = rule.auto_action if auto else ActionKind.FLAG_ONLY
        referrals = [Referral(role=rule.human_role, rule_id=rule.id, reason=rule.name)]
        return Decision(
            post_id=post.id,
            tier=Tier.T0,
            action=action,
            matched_rule_id=rule.id,
            reversible_by=rule.reversible_by,  # issm-only for quarantine
            requires_human_confirm=False,  # T0 is full auto; ISSM runs the SOP
            opens_incident=True,
            strike_delta=0,  # security incident != conduct violation (spec §3)
            contributing=[d for _, d in matches],
            referrals=referrals,
            sla_deadlines=self._deadlines(rule),
            poster_notice=_T0_NOTICE,
            rationale=f"T0 {rule.id} ({rule.name}) at confidence {det.confidence:.2f}",
            confidence=det.confidence,
            model_version=self.env.model_version,
        )

    def _decide_protected(
        self, post: Post, matches: list[tuple[Rule, Detection]], *, co_triggered: bool
    ) -> Decision:
        rule, det = max(matches, key=lambda rd: rd[1].confidence)
        why = "protected-speech guardrail (co-triggered with conduct rule)" if co_triggered else (
            "protected content"
        )
        return Decision(
            post_id=post.id,
            tier=Tier.P,
            action=ActionKind.ROUTE_ONLY,  # never remove (spec PRO-*)
            matched_rule_id=rule.id,
            reversible_by="automation",
            requires_human_confirm=False,
            opens_incident=False,
            strike_delta=0,
            contributing=[d for _, d in matches],
            poster_notice=self._notice(rule, post),
            rationale=f"{why}: {rule.id}",
            confidence=det.confidence,
            model_version=self.env.model_version,
        )

    def _decide_t1(self, post: Post, matches: list[tuple[Rule, Detection]]) -> Decision:
        rule, det = max(matches, key=lambda rd: rd[1].confidence)
        auto = det.confidence >= rule.auto_action_threshold
        # At/above the auto threshold the rule's action fires (hide, or flag_only
        # for CON-007, or label_unverified for CON-008). Below it, flag for review.
        action = rule.auto_action if auto else ActionKind.FLAG_ONLY
        # A hide/label always needs human confirmation; a flag_only rule is
        # already a queue-only disposition.
        needs_confirm = action != ActionKind.FLAG_ONLY
        referrals: list[Referral] = []
        if rule.escalate:
            referrals.append(
                Referral(role=rule.escalate, rule_id=rule.id, reason=f"{rule.name}: escalation")
            )
        return Decision(
            post_id=post.id,
            tier=Tier.T1,
            action=action,
            matched_rule_id=rule.id,
            reversible_by=rule.reversible_by,
            requires_human_confirm=needs_confirm,
            opens_incident=False,
            strike_delta=0,  # strike is applied by a human on confirmation (spec §3)
            contributing=[d for _, d in matches],
            referrals=referrals,
            sla_deadlines=self._deadlines(rule),
            poster_notice=self._notice(rule, post),
            rationale=f"T1 {rule.id} ({rule.name}) at confidence {det.confidence:.2f}",
            confidence=det.confidence,
            model_version=self.env.model_version,
        )

    def _decide_t2(self, post: Post, matches: list[tuple[Rule, Detection]]) -> Decision:
        rule, det = max(matches, key=lambda rd: rd[1].confidence)
        if det.confidence < rule.auto_action_threshold:
            return Decision(
                post_id=post.id,
                tier=Tier.T2,
                action=ActionKind.FLAG_ONLY,
                matched_rule_id=rule.id,
                contributing=[d for _, d in matches],
                poster_notice=self._notice(rule, post),
                rationale=f"T2 {rule.id} below auto threshold; flagged",
                confidence=det.confidence,
                model_version=self.env.model_version,
            )
        return Decision(
            post_id=post.id,
            tier=Tier.T2,
            action=rule.auto_action,
            matched_rule_id=rule.id,
            reversible_by=rule.reversible_by,
            requires_human_confirm=False,
            contributing=[d for _, d in matches],
            sla_deadlines=self._deadlines(rule),
            poster_notice=self._notice(rule, post),
            rationale=f"T2 {rule.id} ({rule.name}) auto action",
            confidence=det.confidence,
            model_version=self.env.model_version,
        )
