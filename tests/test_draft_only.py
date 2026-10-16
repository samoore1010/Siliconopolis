"""Fails if any code path can set an Etsy listing to active."""

import re

import pytest

from pipeline.adapters.etsy.guard import DraftOnlyViolation, assert_draft_only
from pipeline.config import PROJECT_ROOT


def test_guard_rejects_active():
    with pytest.raises(DraftOnlyViolation):
        assert_draft_only({"state": "active"})
    with pytest.raises(DraftOnlyViolation):
        assert_draft_only({"state": "ACTIVE"})
    assert_draft_only({"state": "draft"})
    assert_draft_only({"title": "x"})
    assert_draft_only(None)


def test_mock_client_refuses_active_update():
    from pipeline.adapters.etsy import get_etsy_client

    c = get_etsy_client()
    d = c.create_draft_listing({"title": "t", "state": "draft"})
    with pytest.raises(DraftOnlyViolation):
        c.update_listing(d.listing_id, {"state": "active"})
    with pytest.raises(DraftOnlyViolation):
        c.create_draft_listing({"title": "t", "state": "active"})


ACTIVE_WRITE = re.compile(r"""(['"]state['"]\s*[:=]\s*['"]active['"])|(state\s*=\s*['"]active['"])""")


def test_no_source_path_sends_active():
    """Scan the package: the literal state=active must never appear as a write payload."""
    offenders = []
    for path in (PROJECT_ROOT / "pipeline").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if ACTIVE_WRITE.search(line):
                # the only allowed appearance is comparing an *observed* state (== "active")
                if "==" in line and "payload" not in line:
                    continue
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "active-state write found:\n" + "\n".join(offenders)


def test_allowed_states_is_only_draft():
    from pipeline.adapters.etsy.guard import ALLOWED_LISTING_STATES

    assert ALLOWED_LISTING_STATES == {"draft"}


def test_every_etsy_write_calls_guard():
    """Every create/update method in every Etsy client implementation must call assert_draft_only."""
    for mod in ("mock.py", "live.py"):
        src = (PROJECT_ROOT / "pipeline/adapters/etsy" / mod).read_text()
        for method in ("def create_draft_listing", "def update_listing"):
            body = src.split(method, 1)[1].split("\n    def ", 1)[0]
            assert "assert_draft_only(" in body, f"{mod}: {method} does not call assert_draft_only"
