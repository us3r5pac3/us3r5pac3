"""Records & audit — the non-negotiable invariants (spec §5).

* `WormStore` writes an immutable snapshot of a post (content + thread context +
  metadata + attachments) and refuses to overwrite it. Capture happens BEFORE
  any action; the executor cannot act without a snapshot handle.
* `AuditLog` is append-only JSONL with the exact schema from spec §5.3.

Both are file-backed here for a self-contained system; in a deployment the
files are forwarded to WORM storage out of band. Neither class exposes a delete
path — there is no hard delete in moderation code (spec §5.2).
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import AuditRecord, Post, Snapshot


class SnapshotExists(Exception):
    """Raised on an attempt to overwrite an existing immutable snapshot."""


class WormStore:
    """Write-once snapshot store. One JSON file per post id."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, post_id: int) -> Path:
        return self.root / f"{post_id}.json"

    def has(self, post_id: int) -> bool:
        return self._path(post_id).exists()

    def capture(self, post: Post) -> Snapshot:
        """Capture-before-action. Idempotent per post: a second capture returns
        the original snapshot rather than overwriting it (immutability)."""
        path = self._path(post.id)
        if path.exists():
            return Snapshot.model_validate_json(path.read_text())
        snap = Snapshot(
            post_id=post.id,
            content=post.raw,
            thread_context={"topic_id": post.topic_id, "channel": post.channel},
            metadata={
                "author_edipi": post.author_edipi,
                "affiliation": post.affiliation.value,
                "created_at": post.created_at.isoformat(),
            },
            attachments=post.attachments,
        )
        # x mode: fail loudly if something raced us — never overwrite.
        with path.open("x") as fh:
            fh.write(snap.model_dump_json(indent=2))
        return snap

    def get(self, post_id: int) -> Snapshot:
        return Snapshot.model_validate_json(self._path(post_id).read_text())


class AuditLog:
    """Append-only audit log (spec §5.3). Never truncates or rewrites."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> None:
        with self.path.open("a") as fh:
            fh.write(record.to_jsonl() + "\n")

    def records(self) -> list[AuditRecord]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(AuditRecord.model_validate(json.loads(line)))
        return out

    def for_post(self, post_id: int) -> list[AuditRecord]:
        return [r for r in self.records() if r.target_post == post_id]
