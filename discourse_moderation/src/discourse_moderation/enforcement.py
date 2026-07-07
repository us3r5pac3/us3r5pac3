"""Enforcement state machine (spec §3).

    notice -> warning -> restriction(7d) -> suspension(30d) -> revocation

Automation may execute notice/warning. Restriction and above require a human of
sufficient authority; when automation (or an under-privileged actor) would need
to advance the ladder further, it stops and emits a `Referral` instead — the
personnel action happens outside this system (spec §1, §3).

T0 events do not consume strikes; they open incidents (handled in the engine,
which sets ``strike_delta=0`` for T0). Strikes decay after
``strike_decay_days`` without a violation.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import LadderState, Referral, StrikeLedger

_ORDER = [
    LadderState.NONE,
    LadderState.NOTICE,
    LadderState.WARNING,
    LadderState.RESTRICTION,
    LadderState.SUSPENSION,
    LadderState.REVOCATION,
]

# Minimum actor role authorized to ENTER each state.
_ROLE_RANK = {"automation": 0, "moderator": 1, "lead_moderator": 2, "program_lead": 3}
_STATE_MIN_ROLE = {
    LadderState.NOTICE: "automation",
    LadderState.WARNING: "automation",
    LadderState.RESTRICTION: "moderator",
    LadderState.SUSPENSION: "lead_moderator",
    LadderState.REVOCATION: "program_lead",
}


def _next_state(state: LadderState) -> LadderState:
    idx = _ORDER.index(state)
    return _ORDER[min(idx + 1, len(_ORDER) - 1)]


def record_violation(
    ledger: StrikeLedger, actor_role: str, now: datetime
) -> tuple[StrikeLedger, Referral | None]:
    """Add a strike and advance the ladder as far as ``actor_role`` is allowed.

    Returns the updated ledger and, if the ladder needs a higher authority to
    advance, a referral naming the required role.
    """
    proposed = _next_state(ledger.state)
    min_role = _STATE_MIN_ROLE[proposed]
    authorized = _ROLE_RANK[actor_role] >= _ROLE_RANK[min_role]

    new_strikes = ledger.strikes + 1
    if authorized:
        new_state = proposed
        referral = None
    else:
        new_state = ledger.state  # hold; human must advance it
        referral = Referral(
            role=min_role,
            rule_id="ENF",
            reason=f"advance ladder to {proposed.value} (requires {min_role})",
        )
    updated = ledger.model_copy(
        update={"strikes": new_strikes, "state": new_state, "last_violation_at": now}
    )
    return updated, referral


def apply_decay(ledger: StrikeLedger, now: datetime, decay_days: int) -> StrikeLedger:
    """Clear one strike per elapsed decay window without a violation."""
    if ledger.last_violation_at is None or ledger.strikes == 0:
        return ledger
    windows = (now - ledger.last_violation_at) // timedelta(days=decay_days)
    if windows <= 0:
        return ledger
    strikes = max(0, ledger.strikes - int(windows))
    state = ledger.state if strikes > 0 else LadderState.NONE
    return ledger.model_copy(update={"strikes": strikes, "state": state})


def expunge_strike(ledger: StrikeLedger) -> StrikeLedger:
    """Remove one strike on a successful appeal (spec §4 reversal effect)."""
    strikes = max(0, ledger.strikes - 1)
    state = ledger.state if strikes > 0 else LadderState.NONE
    return ledger.model_copy(update={"strikes": strikes, "state": state})
