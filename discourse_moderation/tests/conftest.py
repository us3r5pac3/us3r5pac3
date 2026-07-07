from __future__ import annotations

import pytest

from discourse_moderation import (
    ActionExecutor,
    AuditLog,
    DryRunClient,
    ModerationEngine,
    Post,
    WormStore,
    load_policy,
)


class FakeClassifier:
    """Test double: returns a preset score for named rules, 0 otherwise."""

    def __init__(self, scores: dict[str, float] | None = None):
        self.scores = scores or {}

    def score(self, text: str, rule_id: str) -> float:  # noqa: ARG002
        return self.scores.get(rule_id, 0.0)


@pytest.fixture
def policy():
    return load_policy()


@pytest.fixture
def engine(policy):
    return ModerationEngine(policy)


def make_post(**overrides) -> Post:
    base = {
        "id": 1,
        "topic_id": 1,
        "author_edipi": "1234567890",
        "affiliation": "CIV",
        "raw": "hello world",
        "channel": "general",
    }
    base.update(overrides)
    return Post.model_validate(base)


@pytest.fixture
def executor(tmp_path):
    client = DryRunClient()
    worm = WormStore(tmp_path / "worm")
    audit = AuditLog(tmp_path / "audit.jsonl")
    return ActionExecutor(client, worm, audit), client, worm, audit
