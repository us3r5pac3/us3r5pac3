"""Appeals (spec §4).

An appeal is reviewed by a *different human* than the original actor (automation
actions may be reviewed by any moderator). A reversal restores content, expunges
the strike, and emits a classifier-feedback event. This module computes the
decision and its effects; the CLI wires the effects to the executor and ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Appeal, utcnow

APPEAL_WINDOW_DAYS = 10  # business-day approximation
DECISION_SLA_DAYS = 3


@dataclass
class AppealEffect:
    restore_content: bool
    expunge_strike: bool
    classifier_feedback: bool


class AppealError(Exception):
    pass


def file_appeal(post_id: int, appellant_edipi: str, rule_id: str) -> Appeal:
    return Appeal(post_id=post_id, appellant_edipi=appellant_edipi, rule_id=rule_id)


def decide_appeal(
    appeal: Appeal,
    *,
    reviewer_edipi: str,
    original_actor_edipi: str | None,
    upheld: bool,
    now: datetime | None = None,
) -> tuple[Appeal, AppealEffect]:
    """Record a decision. Raises if the reviewer is the original human actor."""
    if original_actor_edipi is not None and reviewer_edipi == original_actor_edipi:
        raise AppealError("appeal reviewer must differ from the original actor")

    decided = appeal.model_copy(
        update={"reviewer_edipi": reviewer_edipi, "decided_at": now or utcnow(), "upheld": upheld}
    )
    if upheld:
        effect = AppealEffect(restore_content=False, expunge_strike=False, classifier_feedback=False)
    else:
        # Reversal: content restored, strike expunged, feedback emitted.
        effect = AppealEffect(restore_content=True, expunge_strike=True, classifier_feedback=True)
    return decided, effect


def within_window(action_at: datetime, filed_at: datetime) -> bool:
    return filed_at - action_at <= timedelta(days=APPEAL_WINDOW_DAYS)
