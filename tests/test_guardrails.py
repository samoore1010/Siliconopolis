import pytest

from pipeline.guardrails.blocklist import Blocklist, get_blocklist
from pipeline.guardrails.caps import (
    CapReached,
    check_spend_cap,
    remaining_daily_drafts,
    remaining_weekly_briefs,
)


def test_blocklist_matches_whole_words_case_insensitive():
    bl = Blocklist(["disney", "hello kitty"])
    assert bl.check("A Disney-inspired print") == ["disney"]
    assert bl.check("cute HELLO KITTY planner") == ["hello kitty"]
    assert bl.check("disneyland") == []  # whole word only
    assert bl.is_clean("minimalist fern print")


def test_blocklist_builtin_patterns():
    bl = Blocklist([])
    assert "in the style of" in bl.check("poster in the style of a famous painter")
    assert bl.check("official licensed merch")


def test_repo_blocklist_catches_common_ip():
    bl = get_blocklist()
    assert bl.check_many("Pikachu wall art", ["mickey mouse", "neutral"], "baby yoda print")
    assert bl.is_clean("Minimalist eucalyptus line art print", "digital download", "neutral decor")


def test_spend_cap_raises(session):
    from pipeline.db.models import Run

    session.add(Run(agent="creator", status="success", cost_usd=9.5))
    session.flush()
    with pytest.raises(CapReached) as e:
        check_spend_cap(session, 10.0, 0.6, None)
    assert e.value.cap == "MAX_DAILY_SPEND_USD"
    check_spend_cap(session, 10.0, 0.1, None)  # under the cap is fine


def test_draft_and_brief_caps(session, brief_factory):
    from pipeline.db.models import ProductState

    for _ in range(3):
        brief_factory(session)
    p = brief_factory(session)
    p.state = ProductState.DRAFTED
    session.flush()
    assert remaining_weekly_briefs(session, 20) == 16
    assert remaining_daily_drafts(session, 3) == 2
    assert remaining_daily_drafts(session, 1) == 0
