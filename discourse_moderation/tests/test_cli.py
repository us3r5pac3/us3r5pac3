from __future__ import annotations

import json
from datetime import timedelta

from click.testing import CliRunner
from conftest import make_post

from discourse_moderation import ActionKind, Decision, Tier
from discourse_moderation.__main__ import cli
from discourse_moderation.models import utcnow
from discourse_moderation.state import DecisionStore


def _run(tmp_path, *args):
    runner = CliRunner()
    return runner.invoke(cli, ["--state-dir", str(tmp_path / "state"), *args])


def _first_json(output: str) -> dict:
    """Extract the leading pretty-printed Decision JSON from combined output."""
    start = output.index("{")
    return json.loads(output[start : output.rindex("}") + 1])


def test_evaluate_clean_post_no_action(tmp_path):
    post_file = tmp_path / "clean.json"
    post_file.write_text(make_post(raw="where is the travel template").model_dump_json())
    res = _run(tmp_path, "evaluate", str(post_file))
    assert res.exit_code == 0
    assert _first_json(res.output)["action"] == ActionKind.NONE.value


def test_evaluate_spillage_applies_quarantine(tmp_path):
    post_file = tmp_path / "spill.json"
    post_file.write_text(make_post(raw="this is S//NOFORN").model_dump_json())
    res = _run(tmp_path, "evaluate", str(post_file), "--apply")
    assert res.exit_code == 0, res.output
    out = _first_json(res.output)
    assert out["tier"] == Tier.T0.value
    # Audit + worm were written under the state dir.
    assert (tmp_path / "state" / "audit.jsonl").exists()
    assert (tmp_path / "state" / "worm" / f"{out['post_id']}.json").exists()


def test_expire_t1_restores_unconfirmed(tmp_path):
    state = tmp_path / "state"
    store = DecisionStore(state / "decisions")
    post = make_post(id=77, topic_id=77, raw="hidden conduct")
    decision = Decision(
        post_id=77,
        tier=Tier.T1,
        action=ActionKind.HIDE,
        matched_rule_id="CON-001",
        reversible_by="automation",
        requires_human_confirm=True,
    )
    store.save(post, decision)
    # Backdate the application beyond the 24h TTL.
    store.update(77, applied_at=(utcnow() - timedelta(hours=48)).isoformat())

    res = _run(tmp_path, "expire-t1")
    assert res.exit_code == 0, res.output
    assert "auto-restored post 77" in res.output
    assert store.load(77)["reversed"] is True


def test_confirm_reverse_on_t0_requires_issm(tmp_path):
    post_file = tmp_path / "spill.json"
    post_file.write_text(make_post(id=88, topic_id=88, raw="S//NOFORN data").model_dump_json())
    _run(tmp_path, "evaluate", str(post_file), "--apply")

    # Moderator cannot reverse a T0 quarantine.
    res = _run(tmp_path, "confirm", "88", "--decision", "reverse", "--actor-edipi", "9", "--role", "moderator")
    assert res.exit_code != 0
    # ISSM can.
    res = _run(tmp_path, "confirm", "88", "--decision", "reverse", "--actor-edipi", "9", "--role", "issm")
    assert res.exit_code == 0, res.output
    assert "reversed" in res.output
