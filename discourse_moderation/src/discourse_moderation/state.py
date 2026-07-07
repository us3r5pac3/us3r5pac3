"""Operational (mutable) stores.

Distinct from `records.py`, which holds the *immutable* invariants (WORM
snapshots, append-only audit). This module holds the working state the engine
needs between events: applied decisions awaiting human confirmation, and
per-user strike ledgers. File-backed JSON so the CLI is stateful across runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Decision, LadderState, Post, StrikeLedger, utcnow


class DecisionStore:
    """Applied decisions, keyed by post id, with confirmation state. Lets
    `confirm` and `expire-t1` find the original decision + post to reverse."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, post_id: int) -> Path:
        return self.root / f"{post_id}.json"

    def save(self, post: Post, decision: Decision) -> None:
        payload = {
            "post": post.model_dump(mode="json"),
            "decision": decision.model_dump(mode="json"),
            "applied_at": utcnow().isoformat(),
            "confirmed": False,
            "reversed": False,
        }
        self._path(post.id).write_text(json.dumps(payload, indent=2))

    def load(self, post_id: int) -> dict | None:
        path = self._path(post_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def update(self, post_id: int, **fields) -> None:
        data = self.load(post_id)
        if data is None:
            raise KeyError(post_id)
        data.update(fields)
        self._path(post_id).write_text(json.dumps(data, indent=2))

    def all(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.root.glob("*.json"))]


class LedgerStore:
    """Per-user strike ledgers, keyed by EDIPI."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, edipi: str) -> Path:
        return self.root / f"{edipi}.json"

    def get(self, edipi: str) -> StrikeLedger:
        path = self._path(edipi)
        if not path.exists():
            return StrikeLedger(edipi=edipi, state=LadderState.NONE)
        return StrikeLedger.model_validate_json(path.read_text())

    def put(self, ledger: StrikeLedger) -> None:
        self._path(ledger.edipi).write_text(ledger.model_dump_json(indent=2))
