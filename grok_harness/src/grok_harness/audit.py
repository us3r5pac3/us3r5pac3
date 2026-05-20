from __future__ import annotations

import hashlib
import logging
import logging.handlers
from pathlib import Path
from typing import Any

import structlog


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact_processor(redact_prompts: bool):
    """structlog processor that hashes any field named prompt/response/content."""

    def processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        if not redact_prompts:
            return event_dict
        for key in ("prompt", "response", "content", "messages"):
            if key in event_dict and isinstance(event_dict[key], str):
                event_dict[f"{key}_sha256"] = _hash(event_dict[key])
                event_dict[f"{key}_len"] = len(event_dict[key])
                del event_dict[key]
        return event_dict

    return processor


def configure(audit_path: Path, redact_prompts: bool = True) -> structlog.BoundLogger:
    """Configure an append-only JSONL audit logger.

    IL5 expectations: append-only, never rotated in place (use external
    log forwarder + WORM storage), every record timestamped in UTC, content
    redacted via SHA-256 by default.
    """
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.WatchedFileHandler(audit_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger("grok_harness.audit")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            _redact_processor(redact_prompts),
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger("grok_harness.audit")
