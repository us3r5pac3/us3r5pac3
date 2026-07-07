"""Environment configuration surface (spec §10).

Loaded from ``policy/environment.yaml``. These are per-deployment knobs an ops
owner can tune without touching engine code.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RateLimit(BaseModel):
    n: int = 10
    window_minutes: int = 10


class Environment(BaseModel):
    environment: str = "il5"
    data_ceiling: str = "cui"  # cui | secret | ts-sci
    cil_lexicon: str = "default"
    strike_decay_days: int = 180
    t1_confirm_ttl_hours: int = 24
    max_posts_per_window: RateLimit = Field(default_factory=RateLimit)
    retention_class: dict[str, str] = Field(default_factory=dict)
    model_backend: str = "stub"
    model_version: str = "stub-0"


def load_environment(path: str | Path) -> Environment:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return Environment.model_validate(data)


def default_policy_dir() -> Path:
    """The bundled ``policy/`` directory shipped with the package repo.

    ``config.py`` lives at ``<root>/src/discourse_moderation/config.py``, so the
    project root — which holds ``policy/`` — is two parents up from ``src``.
    """
    return Path(__file__).resolve().parents[2] / "policy"
