from __future__ import annotations

from datetime import datetime, timedelta, timezone

from discourse_moderation.enforcement import apply_decay, expunge_strike, record_violation
from discourse_moderation.models import LadderState, StrikeLedger

NOW = datetime(2026, 7, 7, tzinfo=timezone.utc)


def _ledger():
    return StrikeLedger(edipi="1234567890")


def test_automation_advances_to_notice_then_warning():
    led = _ledger()
    led, ref = record_violation(led, "automation", NOW)
    assert led.state == LadderState.NOTICE and led.strikes == 1 and ref is None
    led, ref = record_violation(led, "automation", NOW)
    assert led.state == LadderState.WARNING and led.strikes == 2 and ref is None


def test_automation_cannot_reach_restriction():
    led = StrikeLedger(edipi="x", strikes=2, state=LadderState.WARNING)
    led, ref = record_violation(led, "automation", NOW)
    # Ladder holds at WARNING; a referral names the required human role.
    assert led.state == LadderState.WARNING
    assert led.strikes == 3
    assert ref is not None and ref.role == "moderator"


def test_moderator_can_advance_to_restriction():
    led = StrikeLedger(edipi="x", strikes=2, state=LadderState.WARNING)
    led, ref = record_violation(led, "moderator", NOW)
    assert led.state == LadderState.RESTRICTION and ref is None


def test_strike_decay_clears_after_window():
    led = StrikeLedger(
        edipi="x", strikes=1, state=LadderState.NOTICE, last_violation_at=NOW - timedelta(days=181)
    )
    led = apply_decay(led, NOW, decay_days=180)
    assert led.strikes == 0 and led.state == LadderState.NONE


def test_no_decay_before_window():
    led = StrikeLedger(
        edipi="x", strikes=1, state=LadderState.NOTICE, last_violation_at=NOW - timedelta(days=10)
    )
    assert apply_decay(led, NOW, decay_days=180).strikes == 1


def test_expunge_on_appeal_reversal():
    led = StrikeLedger(edipi="x", strikes=1, state=LadderState.NOTICE)
    assert expunge_strike(led).strikes == 0
