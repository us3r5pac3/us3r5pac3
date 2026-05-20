from __future__ import annotations

from pathlib import Path

import yaml

from .models import TestSuite


def load_suite(path: Path) -> TestSuite:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return TestSuite.model_validate(data)
