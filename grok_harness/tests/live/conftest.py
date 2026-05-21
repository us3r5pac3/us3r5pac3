"""Live smoke tests — opt-in only, gated by GH_LIVE=1.

Run from a real environment where the configured auth_mode can actually
reach a Keycloak (or skipped), Azure AD, and a Grok 4.3 deployment.
Useful as a one-off acceptance check after a deploy or after wiring
federation; not part of CI.

    GH_LIVE=1 pytest tests/live -v

These tests intentionally do not check correctness of the response —
just that the round-trip happens. The point is wire-shape acceptance
by the real services, which mocks can't prove.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("GH_LIVE"),
        reason="set GH_LIVE=1 to run live tests against real services",
    ),
]
