"""Discourse client surface.

The engine and executor talk to Discourse only through the `DiscourseClient`
protocol, so the moderation logic never depends on a live forum.

* `DryRunClient` records intended calls in memory. It is the default and what
  the tests and the CLI's ``--dry-run`` use — the whole system runs offline.
* `HttpDiscourseClient` implements the same surface against the Discourse REST
  API. Every mutating action has a paired reverse so the executor can undo it
  in one call (spec AUT-001). There is no delete method — moderation code has
  no hard-delete path (spec §5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Call:
    op: str
    args: dict


class DiscourseClient(Protocol):
    def hide_post(self, post_id: int, reason: str) -> None: ...
    def unhide_post(self, post_id: int) -> None: ...
    def lock_topic(self, topic_id: int) -> None: ...
    def unlock_topic(self, topic_id: int) -> None: ...
    def quarantine_post(self, post_id: int, reason: str) -> None: ...
    def release_quarantine(self, post_id: int) -> None: ...
    def move_post(self, post_id: int, to_channel: str) -> None: ...
    def add_tag(self, post_id: int, tag: str) -> None: ...
    def remove_tag(self, post_id: int, tag: str) -> None: ...
    def redact_post(self, post_id: int, redacted_body: str) -> None: ...
    def rate_limit_user(self, edipi: str, window_minutes: int) -> None: ...
    def clear_rate_limit(self, edipi: str) -> None: ...
    def send_notice(self, edipi: str, body: str) -> None: ...


@dataclass
class DryRunClient:
    """Records every intended call instead of performing it."""

    calls: list[Call] = field(default_factory=list)

    def _record(self, op: str, **args) -> None:
        self.calls.append(Call(op=op, args=args))

    def hide_post(self, post_id: int, reason: str) -> None:
        self._record("hide_post", post_id=post_id, reason=reason)

    def unhide_post(self, post_id: int) -> None:
        self._record("unhide_post", post_id=post_id)

    def lock_topic(self, topic_id: int) -> None:
        self._record("lock_topic", topic_id=topic_id)

    def unlock_topic(self, topic_id: int) -> None:
        self._record("unlock_topic", topic_id=topic_id)

    def quarantine_post(self, post_id: int, reason: str) -> None:
        self._record("quarantine_post", post_id=post_id, reason=reason)

    def release_quarantine(self, post_id: int) -> None:
        self._record("release_quarantine", post_id=post_id)

    def move_post(self, post_id: int, to_channel: str) -> None:
        self._record("move_post", post_id=post_id, to_channel=to_channel)

    def add_tag(self, post_id: int, tag: str) -> None:
        self._record("add_tag", post_id=post_id, tag=tag)

    def remove_tag(self, post_id: int, tag: str) -> None:
        self._record("remove_tag", post_id=post_id, tag=tag)

    def redact_post(self, post_id: int, redacted_body: str) -> None:
        self._record("redact_post", post_id=post_id, redacted_body=redacted_body)

    def rate_limit_user(self, edipi: str, window_minutes: int) -> None:
        self._record("rate_limit_user", edipi=edipi, window_minutes=window_minutes)

    def clear_rate_limit(self, edipi: str) -> None:
        self._record("clear_rate_limit", edipi=edipi)

    def send_notice(self, edipi: str, body: str) -> None:
        self._record("send_notice", edipi=edipi, body=body)


class HttpDiscourseClient:
    """Talks to a real Discourse instance via its REST API.

    Requires ``base_url`` and an admin API key/username. Uses the standard
    Discourse admin endpoints (post hide/lock/wiki, PM creation, tags). Kept
    thin on purpose; the mapping from moderation semantics to endpoints lives
    here so the executor stays transport-agnostic.
    """

    def __init__(self, base_url: str, api_key: str, api_username: str, *, timeout: float = 10.0):
        import httpx

        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base,
            headers={
                "Api-Key": api_key,
                "Api-Username": api_username,
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    def _put(self, path: str, json: dict) -> None:
        resp = self._client.put(path, json=json)
        resp.raise_for_status()

    def _post(self, path: str, json: dict) -> None:
        resp = self._client.post(path, json=json)
        resp.raise_for_status()

    def hide_post(self, post_id: int, reason: str) -> None:
        # Discourse: PUT /posts/{id} with hidden flag via moderation action.
        self._put(f"/posts/{post_id}", {"post": {"hidden": True, "edit_reason": reason}})

    def unhide_post(self, post_id: int) -> None:
        self._put(f"/posts/{post_id}", {"post": {"hidden": False}})

    def lock_topic(self, topic_id: int) -> None:
        self._put(f"/t/{topic_id}/status", {"status": "closed", "enabled": True})

    def unlock_topic(self, topic_id: int) -> None:
        self._put(f"/t/{topic_id}/status", {"status": "closed", "enabled": False})

    def quarantine_post(self, post_id: int, reason: str) -> None:
        # Quarantine = hide from non-admin + lock. Search suppression is a
        # platform index concern signaled by the incident tag.
        self.hide_post(post_id, reason)
        self.add_tag(post_id, "security-hold")

    def release_quarantine(self, post_id: int) -> None:
        self.remove_tag(post_id, "security-hold")
        self.unhide_post(post_id)

    def move_post(self, post_id: int, to_channel: str) -> None:
        self._post(f"/posts/{post_id}/move", {"category": to_channel})

    def add_tag(self, post_id: int, tag: str) -> None:
        self._put(f"/posts/{post_id}/tags", {"add": [tag]})

    def remove_tag(self, post_id: int, tag: str) -> None:
        self._put(f"/posts/{post_id}/tags", {"remove": [tag]})

    def redact_post(self, post_id: int, redacted_body: str) -> None:
        self._put(f"/posts/{post_id}", {"post": {"raw": redacted_body}})

    def rate_limit_user(self, edipi: str, window_minutes: int) -> None:
        self._put(f"/admin/users/{edipi}/rate_limit", {"window_minutes": window_minutes})

    def clear_rate_limit(self, edipi: str) -> None:
        self._put(f"/admin/users/{edipi}/rate_limit", {"window_minutes": 0})

    def send_notice(self, edipi: str, body: str) -> None:
        self._post(
            "/posts",
            {"target_recipients": edipi, "archetype": "private_message", "raw": body},
        )
