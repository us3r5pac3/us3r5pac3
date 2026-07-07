"""Load and validate the policy-as-code (spec AUT-006).

The `Policy` object is an immutable, validated view of ``rules.yaml`` +
``environment.yaml``. The engine consumes it; it never re-reads YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import Environment, default_policy_dir, load_environment
from .models import Rule, Tier


class Policy:
    def __init__(self, rules: list[Rule], environment: Environment, version: str):
        self.version = version
        self.environment = environment
        self._by_id: dict[str, Rule] = {}
        for rule in rules:
            if rule.id in self._by_id:
                raise ValueError(f"duplicate rule id in policy: {rule.id}")
            self._by_id[rule.id] = rule
        self.rules = rules

    def get(self, rule_id: str) -> Rule:
        return self._by_id[rule_id]

    def rules_for_detector(self, detector: str) -> list[Rule]:
        return [r for r in self.rules if detector in r.detectors]

    def rules_in_tier(self, tier: Tier) -> list[Rule]:
        return [r for r in self.rules if r.tier == tier]


def load_policy(policy_dir: str | Path | None = None) -> Policy:
    directory = Path(policy_dir) if policy_dir else default_policy_dir()
    raw = yaml.safe_load((directory / "rules.yaml").read_text()) or {}
    version = str(raw.get("version", "0"))
    rules = [Rule.model_validate(r) for r in raw.get("rules", [])]
    if not rules:
        raise ValueError(f"no rules found in {directory / 'rules.yaml'}")
    environment = load_environment(directory / "environment.yaml")
    return Policy(rules=rules, environment=environment, version=version)
