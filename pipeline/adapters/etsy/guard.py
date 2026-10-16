"""Draft-only guardrail for v1.

Every Etsy *write* passes through `assert_draft_only`. The only listing state this code base may
send to Etsy is "draft". tests/test_draft_only.py also scans the source tree so no other module can
introduce an active-state write.
"""

from __future__ import annotations

from typing import Any

ALLOWED_LISTING_STATES = frozenset({"draft"})


class DraftOnlyViolation(RuntimeError):
    pass


def assert_draft_only(payload: dict[str, Any] | None) -> None:
    if not payload:
        return
    state = payload.get("state")
    if state is not None and str(state).lower() not in ALLOWED_LISTING_STATES:
        raise DraftOnlyViolation(
            f"refusing to send listing state={state!r}: v1 is draft-only; publishing is the human step"
        )
