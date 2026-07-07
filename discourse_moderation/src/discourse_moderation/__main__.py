"""``discourse-mod`` CLI.

Ties the pieces together for operators and CI:

    discourse-mod evaluate post.json                 # pure decision, no side effects
    discourse-mod evaluate post.json --apply         # snapshot + act + audit
    discourse-mod process-webhook payload.json --apply
    discourse-mod confirm 42 --decision confirm --actor-edipi 1234567890 --role moderator
    discourse-mod expire-t1                           # auto-restore unconfirmed T1 hides
    discourse-mod appeal-decide 42 --reviewer ... --original-actor ... --outcome reverse

State (applied decisions, ledgers) lives under ``--state-dir`` (default
``./.modstate``) so confirmation, TTL expiry, and appeals can find prior actions.
Actions default to a dry run; pass ``--live`` to hit a real Discourse instance.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import click

from .actions import ActionExecutor
from .appeals import decide_appeal, file_appeal
from .discourse import DryRunClient, HttpDiscourseClient
from .engine import ModerationEngine
from .enforcement import expunge_strike, record_violation
from .models import ActionKind, Decision, Post, Tier, utcnow
from .policy import load_policy
from .records import AuditLog, WormStore
from .state import DecisionStore, LedgerStore


class Context:
    def __init__(self, policy_dir: str | None, state_dir: str, live: bool):
        self.policy = load_policy(policy_dir)
        self.engine = ModerationEngine(self.policy)
        root = Path(state_dir)
        self.worm = WormStore(root / "worm")
        self.audit = AuditLog(root / "audit.jsonl")
        self.decisions = DecisionStore(root / "decisions")
        self.ledgers = LedgerStore(root / "ledgers")
        self.appeals_dir = root / "appeals"
        self.appeals_dir.mkdir(parents=True, exist_ok=True)
        self.client = self._build_client(live)
        self.executor = ActionExecutor(self.client, self.worm, self.audit)

    def _build_client(self, live: bool):
        if not live:
            return DryRunClient()
        return HttpDiscourseClient(
            base_url=os.environ["DISCOURSE_BASE_URL"],
            api_key=os.environ["DISCOURSE_API_KEY"],
            api_username=os.environ.get("DISCOURSE_API_USERNAME", "system"),
        )


def _emit(decision: Decision) -> None:
    click.echo(decision.model_dump_json(indent=2))


def webhook_to_post(payload: dict) -> Post:
    """Map a Discourse ``post_created``/``post_edited`` webhook to a `Post`."""
    p = payload.get("post", payload)
    return Post.model_validate(
        {
            "id": p["id"],
            "topic_id": p.get("topic_id", p["id"]),
            "author_edipi": str(p.get("username") or p.get("user_id") or "unknown"),
            "affiliation": p.get("affiliation", "CIV"),
            "raw": p.get("raw", ""),
            "channel": p.get("category_slug", "general"),
        }
    )


@click.group()
@click.option("--policy-dir", default=None, help="Directory holding rules.yaml + environment.yaml")
@click.option("--state-dir", default=".modstate", show_default=True)
@click.option("--live", is_flag=True, help="Act against a real Discourse instance (env-configured)")
@click.pass_context
def cli(ctx: click.Context, policy_dir: str | None, state_dir: str, live: bool):
    ctx.obj = Context(policy_dir, state_dir, live)


def _evaluate_and_maybe_apply(ctx: Context, post: Post, apply: bool) -> Decision:
    decision = ctx.engine.evaluate(post)
    _emit(decision)
    if apply and decision.action != ActionKind.NONE:
        ctx.executor.apply(post, decision)
        ctx.decisions.save(post, decision)
        click.echo(f"applied: {decision.action.value} on post {post.id}", err=True)
    return decision


@cli.command()
@click.argument("post_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--apply", is_flag=True, help="Execute the decision (snapshot + act + audit)")
@click.pass_obj
def evaluate(ctx: Context, post_json: str, apply: bool):
    """Evaluate a post given as JSON. Prints the Decision."""
    post = Post.model_validate_json(Path(post_json).read_text())
    _evaluate_and_maybe_apply(ctx, post, apply)


@cli.command("process-webhook")
@click.argument("payload_json", type=click.Path(exists=True, dir_okay=False))
@click.option("--apply", is_flag=True)
@click.pass_obj
def process_webhook(ctx: Context, payload_json: str, apply: bool):
    """Evaluate a Discourse webhook payload."""
    payload = json.loads(Path(payload_json).read_text())
    post = webhook_to_post(payload)
    _evaluate_and_maybe_apply(ctx, post, apply)


@cli.command()
@click.argument("post_id", type=int)
@click.option("--decision", "verdict", type=click.Choice(["confirm", "reverse"]), required=True)
@click.option("--actor-edipi", required=True)
@click.option("--role", default="moderator", show_default=True)
@click.pass_obj
def confirm(ctx: Context, post_id: int, verdict: str, actor_edipi: str, role: str):
    """Human confirmation of a T1 auto-hide: confirm (strike) or reverse (restore)."""
    data = ctx.decisions.load(post_id)
    if data is None:
        raise click.ClickException(f"no applied decision for post {post_id}")
    post = Post.model_validate(data["post"])
    decision = Decision.model_validate(data["decision"])

    if verdict == "reverse":
        ctx.executor.reverse(post, decision, actor_edipi=actor_edipi, actor_role=role)
        ctx.decisions.update(post_id, reversed=True, confirmed=True)
        click.echo(f"reversed {decision.action.value} on post {post_id}")
        return

    # confirm: sustain the action and, for T1 conduct, apply a strike.
    ctx.decisions.update(post_id, confirmed=True)
    if decision.tier == Tier.T1 and decision.requires_human_confirm:
        ledger = ctx.ledgers.get(post.author_edipi)
        ledger, referral = record_violation(ledger, role, utcnow())
        ctx.ledgers.put(ledger)
        click.echo(f"strike recorded: {post.author_edipi} -> {ledger.strikes} ({ledger.state.value})")
        if referral is not None:
            click.echo(f"referral: {referral.reason}")
    else:
        click.echo(f"confirmed {decision.action.value} on post {post_id}")


@cli.command("expire-t1")
@click.pass_obj
def expire_t1(ctx: Context):
    """Auto-restore T1 auto-hides not confirmed within the TTL (spec AUT-003)."""
    ttl = timedelta(hours=ctx.policy.environment.t1_confirm_ttl_hours)
    now = utcnow()
    restored = 0
    for data in ctx.decisions.all():
        decision = Decision.model_validate(data["decision"])
        if decision.tier != Tier.T1 or not decision.requires_human_confirm:
            continue
        if data.get("confirmed") or data.get("reversed"):
            continue
        applied_at = datetime.fromisoformat(data["applied_at"])
        if now - applied_at < ttl:
            continue
        post = Post.model_validate(data["post"])
        ctx.executor.reverse(post, decision, actor_edipi="automation", actor_role="automation")
        ctx.decisions.update(post.id, reversed=True)
        click.echo(f"auto-restored post {post.id} (unconfirmed past {ttl})")
        restored += 1
    click.echo(f"{restored} post(s) auto-restored", err=True)


@cli.command()
@click.argument("post_id", type=int)
@click.option("--appellant", required=True)
@click.pass_obj
def appeal(ctx: Context, post_id: int, appellant: str):
    """File an appeal against the action on a post."""
    data = ctx.decisions.load(post_id)
    if data is None:
        raise click.ClickException(f"no applied decision for post {post_id}")
    decision = Decision.model_validate(data["decision"])
    ap = file_appeal(post_id, appellant, decision.matched_rule_id or "")
    (ctx.appeals_dir / f"{post_id}.json").write_text(ap.model_dump_json(indent=2))
    click.echo(f"appeal filed for post {post_id} (rule {ap.rule_id})")


@cli.command("appeal-decide")
@click.argument("post_id", type=int)
@click.option("--reviewer", required=True)
@click.option("--original-actor", default=None)
@click.option("--outcome", type=click.Choice(["uphold", "reverse"]), required=True)
@click.pass_obj
def appeal_decide(ctx: Context, post_id: int, reviewer: str, original_actor: str | None, outcome: str):
    """Decide an appeal. A reversal restores content, expunges the strike, and
    emits a classifier-feedback event (spec §4)."""
    ap_path = ctx.appeals_dir / f"{post_id}.json"
    if not ap_path.exists():
        raise click.ClickException(f"no appeal on file for post {post_id}")
    from .models import Appeal

    ap = Appeal.model_validate_json(ap_path.read_text())
    decided, effect = decide_appeal(
        ap, reviewer_edipi=reviewer, original_actor_edipi=original_actor, upheld=(outcome == "uphold")
    )
    ap_path.write_text(decided.model_dump_json(indent=2))

    if effect.restore_content:
        data = ctx.decisions.load(post_id)
        post = Post.model_validate(data["post"])
        decision = Decision.model_validate(data["decision"])
        ctx.executor.reverse(post, decision, actor_edipi=reviewer, actor_role="lead_moderator")
        ctx.decisions.update(post_id, reversed=True)
        ledger = ctx.ledgers.get(post.author_edipi)
        ctx.ledgers.put(expunge_strike(ledger))
        click.echo("content restored; strike expunged; classifier_feedback emitted")
    else:
        click.echo("action upheld")


if __name__ == "__main__":
    cli()
