"""Policy-as-code moderation automation for a CAC-gated Discourse forum.

The pipeline: a `Post` is evaluated by `ModerationEngine` (pure) into a
`Decision`; `ActionExecutor` captures a WORM snapshot, applies a reversible
Discourse action, and writes an append-only audit record. See the module
docstrings and README for the mapping back to the policy spec.
"""

from __future__ import annotations

from .actions import ActionExecutor
from .config import Environment, load_environment
from .detectors import Classifier, StubClassifier
from .discourse import DryRunClient, HttpDiscourseClient
from .engine import ModerationEngine
from .models import ActionKind, Decision, Post, Tier
from .policy import Policy, load_policy
from .records import AuditLog, WormStore

__all__ = [
    "ActionExecutor",
    "ActionKind",
    "AuditLog",
    "Classifier",
    "Decision",
    "DryRunClient",
    "Environment",
    "HttpDiscourseClient",
    "ModerationEngine",
    "Policy",
    "Post",
    "StubClassifier",
    "Tier",
    "WormStore",
    "load_environment",
    "load_policy",
]
