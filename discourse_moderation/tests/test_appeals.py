from __future__ import annotations

import pytest

from discourse_moderation.appeals import AppealError, decide_appeal, file_appeal


def test_reviewer_must_differ_from_original_actor():
    ap = file_appeal(post_id=1, appellant_edipi="1111111111", rule_id="CON-001")
    with pytest.raises(AppealError):
        decide_appeal(ap, reviewer_edipi="9", original_actor_edipi="9", upheld=False)


def test_automation_action_reviewable_by_any_moderator():
    ap = file_appeal(post_id=1, appellant_edipi="1111111111", rule_id="CON-001")
    decided, effect = decide_appeal(
        ap, reviewer_edipi="mod-7", original_actor_edipi=None, upheld=False
    )
    assert decided.upheld is False
    assert effect.restore_content and effect.expunge_strike and effect.classifier_feedback


def test_upheld_appeal_has_no_effect():
    ap = file_appeal(post_id=1, appellant_edipi="1111111111", rule_id="CON-001")
    _, effect = decide_appeal(ap, reviewer_edipi="mod-7", original_actor_edipi="mod-1", upheld=True)
    assert not effect.restore_content and not effect.expunge_strike
