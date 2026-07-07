"""Domain model for the moderation engine.

Everything the engine reasons about is a typed pydantic object. The two most
important types are `Decision` — the pure output of the engine, computed with no
side effects — and `AuditRecord`, whose schema is fixed by the policy spec §5.3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Tier(str, Enum):
    """Severity tier — determines what automation may do without a human (spec §2)."""

    T0 = "T0"  # security incident: full auto quarantine/lock/notify
    T1 = "T1"  # prohibited conduct: auto-hide + mandatory human confirm
    T2 = "T2"  # usage: full auto soft actions
    P = "P"  # protected: automation may only route, removal forbidden


class Affiliation(str, Enum):
    MIL = "MIL"
    CIV = "CIV"
    CTR = "CTR"


class ActionKind(str, Enum):
    """A content action the automation can take. There is deliberately no
    ``delete`` — moderation code has no hard-delete path (spec §5.2)."""

    NONE = "none"
    FLAG_ONLY = "flag_only"  # queue for humans, no content action
    ROUTE_ONLY = "route_only"  # protected content: notify/route, leave in place
    QUARANTINE = "quarantine"  # hide from all non-admin, lock, suppress from search
    HIDE = "hide"
    LOCK = "lock"
    MOVE = "move"
    TAG = "tag"
    RATE_LIMIT = "rate_limit"
    REDACT = "redact"
    LABEL_UNVERIFIED = "label_unverified"


class Attachment(BaseModel):
    filename: str
    content_type: str
    size_bytes: int


class Post(BaseModel):
    """A Discourse post presented for moderation."""

    id: int
    topic_id: int
    author_edipi: str
    affiliation: Affiliation
    raw: str
    channel: str = "general"
    created_at: datetime = Field(default_factory=utcnow)
    attachments: list[Attachment] = Field(default_factory=list)
    # Discourse/thread signals used by usage detectors.
    topic_archived: bool = False
    is_duplicate: bool = False
    author_recent_post_count: int = 0
    has_required_tags: bool = True


class Rule(BaseModel):
    """One row of the policy, loaded from rules.yaml."""

    id: str
    name: str
    tier: Tier
    detectors: list[str] = Field(default_factory=list)
    auto_action: ActionKind
    human_role: str = "none"
    reversible_by: str = "automation"  # automation | moderator | issm
    sla_minutes: int | None = None
    authority: str = ""
    bypass_strikes: bool = False
    never_remove: bool = False
    escalate: str | None = None
    auto_action_threshold: float = 0.85
    flag_only_threshold: float = 0.5
    policy_excerpt: str = ""


class Detection(BaseModel):
    """A detector's finding that a rule may apply."""

    rule_id: str
    tier: Tier
    confidence: float
    evidence: list[str] = Field(default_factory=list)


class Referral(BaseModel):
    """A personnel/security routing artifact. The system generates it; the
    action itself happens outside the system (spec §1 core invariant, §3)."""

    role: str
    rule_id: str
    reason: str


class SLADeadline(BaseModel):
    """An alert/confirm timer the surrounding platform must honor (spec §8)."""

    kind: str  # e.g. "issm_alert", "t1_confirm", "privacy_notify"
    due_at: datetime


class Decision(BaseModel):
    """The pure result of evaluating a post. No side effects have happened yet."""

    post_id: int
    tier: Tier | None = None
    action: ActionKind = ActionKind.NONE
    matched_rule_id: str | None = None
    reversible_by: str = "automation"
    requires_human_confirm: bool = False
    opens_incident: bool = False
    strike_delta: int = 0
    contributing: list[Detection] = Field(default_factory=list)
    referrals: list[Referral] = Field(default_factory=list)
    sla_deadlines: list[SLADeadline] = Field(default_factory=list)
    poster_notice: str | None = None
    rationale: str = ""
    confidence: float = 0.0
    model_version: str | None = None


class AuditRecord(BaseModel):
    """Append-only audit entry. Schema fixed by spec §5.3."""

    timestamp: datetime = Field(default_factory=utcnow)
    actor: str  # "human:<edipi>" or "<rule_id>+<model_version>"
    target_post: int
    action: str
    rationale: str
    confidence_score: float
    appeal_status: str = "none"

    def to_jsonl(self) -> str:
        return self.model_dump_json()


class Snapshot(BaseModel):
    """Immutable capture written to the WORM store BEFORE any action (spec §5.1)."""

    post_id: int
    captured_at: datetime = Field(default_factory=utcnow)
    content: str
    thread_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)


# ---- Enforcement (spec §3) ---------------------------------------------------


class LadderState(str, Enum):
    NONE = "none"
    NOTICE = "notice"
    WARNING = "warning"
    RESTRICTION = "restriction"  # 7d, read-only
    SUSPENSION = "suspension"  # 30d
    REVOCATION = "revocation"


class StrikeLedger(BaseModel):
    edipi: str
    strikes: int = 0
    state: LadderState = LadderState.NONE
    last_violation_at: datetime | None = None


# ---- Appeals (spec §4) -------------------------------------------------------


class Appeal(BaseModel):
    post_id: int
    appellant_edipi: str
    rule_id: str
    filed_at: datetime = Field(default_factory=utcnow)
    reviewer_edipi: str | None = None
    decided_at: datetime | None = None
    upheld: bool | None = None  # None = pending; True = action stands; False = reversed
