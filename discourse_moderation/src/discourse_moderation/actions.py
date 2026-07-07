"""Action executor — turns a `Decision` into reversible Discourse operations.

Invariants enforced structurally (spec §5, AUT-001):

* **Capture-before-action.** `apply` writes the WORM snapshot before touching
  the post. If the snapshot write fails, no action is taken.
* **Reversibility.** Every applied action has a `reverse`. T0 quarantine is
  reversible by the ISSM role only; T1 by a moderator; T2 by automation.
* **No hard delete.** There is no delete path anywhere in this module.
"""

from __future__ import annotations

import re

from .discourse import DiscourseClient
from .models import ActionKind, AuditRecord, Decision, Post, Tier
from .records import AuditLog, WormStore

# Roles allowed to reverse an action, keyed by the decision's reversible_by.
_REVERSAL_ROLES = {
    "automation": {"automation", "moderator", "lead_moderator", "issm"},
    "moderator": {"moderator", "lead_moderator", "issm"},
    "issm": {"issm"},
}

_SECRET_MASK_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END[^\n]*-----", re.S),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)(mongodb|postgres|mysql|redis)://[^\s]+:[^\s]+@"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
]


def _redact_secrets(text: str) -> str:
    out = text
    for pat in _SECRET_MASK_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


class NotAuthorized(Exception):
    pass


class ActionExecutor:
    def __init__(self, client: DiscourseClient, worm: WormStore, audit: AuditLog):
        self.client = client
        self.worm = worm
        self.audit = audit

    # ---- apply ---------------------------------------------------------------

    def apply(self, post: Post, decision: Decision, *, actor: str = "automation") -> None:
        """Execute the decision. Snapshot first, then act, then audit."""
        # Capture-before-action: this must succeed before any mutation.
        self.worm.capture(post)

        action = decision.action
        reason = decision.rationale

        if action == ActionKind.QUARANTINE:
            self.client.quarantine_post(post.id, reason)
            self.client.lock_topic(post.topic_id)
        elif action == ActionKind.HIDE:
            self.client.hide_post(post.id, reason)
        elif action == ActionKind.LOCK:
            self.client.lock_topic(post.topic_id)
        elif action == ActionKind.MOVE:
            self.client.move_post(post.id, "triage")
        elif action == ActionKind.TAG:
            self.client.add_tag(post.id, "moderation")
        elif action == ActionKind.RATE_LIMIT:
            self.client.rate_limit_user(post.author_edipi, window_minutes=10)
        elif action == ActionKind.REDACT:
            self.client.redact_post(post.id, _redact_secrets(post.raw))
        elif action == ActionKind.LABEL_UNVERIFIED:
            self.client.add_tag(post.id, "unverified")
        # FLAG_ONLY, ROUTE_ONLY, NONE: no content mutation.

        # Transparency: poster always gets a notice unless nothing happened
        # (spec §4). T0 uses the generic "held for security review" wording.
        if decision.poster_notice and action != ActionKind.NONE:
            self.client.send_notice(post.author_edipi, decision.poster_notice)

        # Route referrals (SLA-timed alerts to roles) — artifacts, executed
        # outside the system, but the notice is emitted here.
        for ref in decision.referrals:
            self.client.send_notice(f"role:{ref.role}", f"[{ref.rule_id}] {ref.reason}")

        self.audit.append(self._record(post, decision, action.value, actor))

    # ---- reverse -------------------------------------------------------------

    def reverse(self, post: Post, decision: Decision, *, actor_edipi: str, actor_role: str) -> None:
        """Undo an applied action in one operation. Authorization is checked
        against the decision's ``reversible_by`` (spec AUT-001)."""
        allowed = _REVERSAL_ROLES.get(decision.reversible_by, set())
        if actor_role not in allowed:
            raise NotAuthorized(
                f"role {actor_role!r} may not reverse a {decision.reversible_by}-reversible action"
            )

        action = decision.action
        if action == ActionKind.QUARANTINE:
            self.client.release_quarantine(post.id)
            self.client.unlock_topic(post.topic_id)
        elif action == ActionKind.HIDE:
            self.client.unhide_post(post.id)
        elif action == ActionKind.LOCK:
            self.client.unlock_topic(post.topic_id)
        elif action == ActionKind.MOVE:
            self.client.move_post(post.id, post.channel)  # back to original
        elif action == ActionKind.TAG:
            self.client.remove_tag(post.id, "moderation")
        elif action == ActionKind.RATE_LIMIT:
            self.client.clear_rate_limit(post.author_edipi)
        elif action == ActionKind.REDACT:
            self.client.redact_post(post.id, self.worm.get(post.id).content)  # restore original
        elif action == ActionKind.LABEL_UNVERIFIED:
            self.client.remove_tag(post.id, "unverified")

        rec = self._record(post, decision, f"reverse:{action.value}", f"human:{actor_edipi}")
        self.audit.append(rec)

    # ---- audit helper --------------------------------------------------------

    def _record(self, post: Post, decision: Decision, action: str, actor: str) -> AuditRecord:
        if actor == "automation":
            actor_str = f"{decision.matched_rule_id}+{decision.model_version}"
        else:
            actor_str = actor
        appeal_status = "held" if decision.tier == Tier.T0 else "none"
        return AuditRecord(
            actor=actor_str,
            target_post=post.id,
            action=action,
            rationale=decision.rationale,
            confidence_score=decision.confidence,
            appeal_status=appeal_status,
        )
