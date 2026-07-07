"""Detectors map post content/metadata to rule detections.

Two families:

* **Deterministic** detectors (classification markings, secrets, PII, OPSEC
  lexicon, usage signals) run offline with regex/entropy/lexicon logic and
  return a high confidence on a clear match.
* **Classifier** detectors (`classifier_conduct`, `classifier_protected`) route
  through a pluggable `Classifier`. The bundled `StubClassifier` scores 0.0, so
  on its own nothing is auto-hidden — a real, accreditation-boundary model
  (spec AUT-004) is dropped in without touching the engine.

Every detector has the same signature and is looked up by name from a rule's
``detectors`` list, so adding detection for a rule is a YAML edit plus (for a
new technique) one function here.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Protocol

from .config import Environment
from .models import Detection, Post, Rule

# ---- Classifier seam (spec AUT-004) -----------------------------------------


class Classifier(Protocol):
    """A platform-hosted model kept inside the accreditation boundary. Returns a
    0..1 confidence that ``rule_id`` applies to ``text``."""

    def score(self, text: str, rule_id: str) -> float: ...


class StubClassifier:
    """Conservative default: never asserts a conduct/protected match. Keeps the
    engine fully offline and ensures nothing is auto-hidden on a stub score."""

    def score(self, text: str, rule_id: str) -> float:  # noqa: ARG002
        return 0.0


# ---- Deterministic lexicons and patterns ------------------------------------

_CLASSIFICATION_RE = re.compile(
    r"""(?ix)
    \b(
        (TS|S|C|U)//[A-Z/]+          # portion markings: S//NF, TS//SI//...
      | \bTOP\s+SECRET\b
      | \bSECRET//[A-Z]+
      | \bSCI\b | \bSAP\b | \bNOFORN\b | \bHCS\b | \bGAMMA\b
    )
    """,
)

_CUI_RE = re.compile(r"(?i)\bCUI(//[A-Z/]+)?\b|\bCONTROLLED\s+UNCLASSIFIED\b")

_OPSEC_LEXICON = [
    "convoy departs",
    "wheels up at",
    "patrol route",
    "unit strength",
    "deployment date",
    "flight manifest",
]

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S{6,}"),
    re.compile(r"(?i)(mongodb|postgres|mysql|redis)://[^\s]+:[^\s]+@"),  # conn strings
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),  # slack tokens
    re.compile(r"ghp_[0-9A-Za-z]{36}"),  # github PAT
]

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EDIPI_RE = re.compile(r"\b\d{10}\b")
_DOB_RE = re.compile(r"\b(0?[1-9]|1[0-2])[/-](0?[1-9]|[12]\d|3[01])[/-](19|20)\d{2}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")

_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024
_ATTACHMENT_ALLOWED = {
    "image/png",
    "image/jpeg",
    "application/pdf",
    "text/plain",
}


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _has_high_entropy_token(text: str, *, min_len: int = 20, min_entropy: float = 3.5) -> bool:
    for token in re.split(r"\s+", text):
        if len(token) >= min_len and _shannon_entropy(token) >= min_entropy:
            return True
    return False


# ---- Detector functions ------------------------------------------------------

DetectorFn = Callable[[Post, Rule, Environment, Classifier], Detection | None]


def _match(rule: Rule, confidence: float, evidence: list[str]) -> Detection | None:
    if confidence <= 0:
        return None
    return Detection(rule_id=rule.id, tier=rule.tier, confidence=confidence, evidence=evidence)


def detect_classification(post: Post, rule: Rule, env: Environment, clf: Classifier):
    hits = [m.group(0) for m in _CLASSIFICATION_RE.finditer(post.raw)]
    return _match(rule, 0.97 if hits else 0.0, hits[:5])


def detect_cui(post: Post, rule: Rule, env: Environment, clf: Classifier):
    # Only a violation if CUI markings exceed the environment's ceiling.
    if env.data_ceiling in ("secret", "ts-sci"):
        return None  # environment is accredited above CUI; CUI is in-bounds
    hits = [m.group(0) for m in _CUI_RE.finditer(post.raw)]
    return _match(rule, 0.9 if hits else 0.0, hits[:5])


def detect_secrets(post: Post, rule: Rule, env: Environment, clf: Classifier):
    evidence: list[str] = []
    for pat in _SECRET_PATTERNS:
        m = pat.search(post.raw)
        if m:
            evidence.append(pat.pattern)
    confidence = 0.95 if evidence else 0.0
    if not evidence and _has_high_entropy_token(post.raw):
        confidence, evidence = 0.6, ["high-entropy token"]
    return _match(rule, confidence, evidence)


def detect_pii(post: Post, rule: Rule, env: Environment, clf: Classifier):
    evidence: list[str] = []
    if _SSN_RE.search(post.raw):
        evidence.append("SSN pattern")
    if _EDIPI_RE.search(post.raw) and _DOB_RE.search(post.raw):
        evidence.append("EDIPI+DoB combo")
    if _CC_RE.search(post.raw):
        evidence.append("financial account pattern")
    return _match(rule, 0.9 if evidence else 0.0, evidence)


def detect_opsec(post: Post, rule: Rule, env: Environment, clf: Classifier):
    lowered = post.raw.lower()
    hits = [term for term in _OPSEC_LEXICON if term in lowered]
    return _match(rule, 0.85 if hits else 0.0, hits)


def detect_flood(post: Post, rule: Rule, env: Environment, clf: Classifier):
    limit = env.max_posts_per_window.n
    if post.is_duplicate:
        return _match(rule, 0.9, ["duplicate content"])
    if post.author_recent_post_count > limit:
        return _match(rule, 0.8, [f"{post.author_recent_post_count} posts in window > {limit}"])
    return None


def detect_necro(post: Post, rule: Rule, env: Environment, clf: Classifier):
    return _match(rule, 0.9 if post.topic_archived else 0.0, ["reply to archived topic"])


def detect_attachment(post: Post, rule: Rule, env: Environment, clf: Classifier):
    for att in post.attachments:
        if att.size_bytes > _ATTACHMENT_MAX_BYTES:
            return _match(rule, 0.95, [f"{att.filename}: {att.size_bytes} bytes over limit"])
        if att.content_type not in _ATTACHMENT_ALLOWED:
            return _match(rule, 0.85, [f"{att.filename}: unsupported {att.content_type}"])
    return None


def detect_template(post: Post, rule: Rule, env: Environment, clf: Classifier):
    return _match(rule, 0.8 if not post.has_required_tags else 0.0, ["missing required tags"])


def detect_offtopic(post: Post, rule: Rule, env: Environment, clf: Classifier):
    # Off-topic / hijacking requires a topic model we don't ship offline. The
    # rule is declared; detection defers to the classifier when one is wired.
    score = clf.score(post.raw, rule.id)
    return _match(rule, score, ["classifier off-topic score"] if score else [])


def detect_conduct(post: Post, rule: Rule, env: Environment, clf: Classifier):
    score = clf.score(post.raw, rule.id)
    return _match(rule, score, [f"classifier score for {rule.id}"] if score else [])


def detect_protected(post: Post, rule: Rule, env: Environment, clf: Classifier):
    score = clf.score(post.raw, rule.id)
    return _match(rule, score, [f"classifier score for {rule.id}"] if score else [])


DETECTORS: dict[str, DetectorFn] = {
    "classification_markings": detect_classification,
    "cui_markings": detect_cui,
    "secrets": detect_secrets,
    "pii": detect_pii,
    "opsec_lexicon": detect_opsec,
    "usage_flood": detect_flood,
    "usage_necro": detect_necro,
    "usage_attachment": detect_attachment,
    "usage_template": detect_template,
    "usage_offtopic": detect_offtopic,
    "classifier_conduct": detect_conduct,
    "classifier_protected": detect_protected,
}


def run_detectors(
    post: Post, rule: Rule, env: Environment, clf: Classifier
) -> list[Detection]:
    """Run every detector declared for a rule; return the detections that fired."""
    out: list[Detection] = []
    for name in rule.detectors:
        fn = DETECTORS.get(name)
        if fn is None:
            continue
        det = fn(post, rule, env, clf)
        if det is not None:
            out.append(det)
    return out
