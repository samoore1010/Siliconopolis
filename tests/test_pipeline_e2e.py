"""Full pipeline in mock mode: brief -> created -> approved/rejected -> drafted, plus caps and status."""

from __future__ import annotations

from sqlalchemy import select

from pipeline.db import session_scope
from pipeline.db.models import Product, ProductState, ResearchNote, Run


def _states():
    with session_scope() as s:
        return [p.state for p in s.execute(select(Product).order_by(Product.id)).scalars()]


def test_run_all_moves_products_to_drafted(monkeypatch):
    from pipeline.__main__ import main

    assert main(["run-all"]) == 0
    states = _states()
    assert states, "researcher produced no briefs"
    assert ProductState.DRAFTED in states
    assert not any(st in (ProductState.CREATED, ProductState.APPROVED) for st in states), states
    with session_scope() as s:
        runs = s.execute(select(Run).order_by(Run.id)).scalars().all()
        assert [r.agent for r in runs] == ["researcher", "creator", "qa", "lister", "analyst", "fulfiller"]
        assert all(r.status == "success" and r.ended_at for r in runs)
        drafted = s.execute(select(Product).where(Product.state == ProductState.DRAFTED)).scalars().all()
        for p in drafted:
            assert p.etsy_listing_id and p.listing_json["state"] == "draft"
            assert len(p.listing_json["tags"]) == 13 and len(p.listing_json["title"]) <= 140
            assert "AI tools" in p.listing_json["description"]
            assert len(p.image_paths) >= 3 and p.file_paths
            assert p.locked_by is None
        notes = s.execute(select(ResearchNote)).scalars().all()
        assert {n.source for n in notes} == {"researcher", "analyst", "market"}


def test_rerun_is_idempotent():
    from pipeline.__main__ import main

    assert main(["run", "daily"]) == 0  # nothing to do yet
    assert main(["run", "researcher"]) == 0
    n_briefs = _states().count(ProductState.BRIEF)
    for _ in range(3):  # batch size is 10; the final run must find nothing to claim
        assert main(["run", "creator"]) == 0
    with session_scope() as s:
        creator_runs = s.execute(select(Run).where(Run.agent == "creator").order_by(Run.id)).scalars().all()
        assert sum(r.items_processed for r in creator_runs) == n_briefs
        assert creator_runs[-1].items_processed == 0
        assert _states().count(ProductState.BRIEF) == 0


def test_daily_draft_cap_stops_lister(monkeypatch, brief_factory):
    monkeypatch.setenv("DAILY_DRAFT_CAP", "1")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    from pipeline.__main__ import main

    with session_scope() as s:
        for i in range(3):
            brief_factory(s, concept=f"Original concept {i}: motif {i}")
    assert main(["run", "creator"]) == 0
    assert main(["run", "qa"]) == 0
    approved = _states().count(ProductState.APPROVED)
    assert approved >= 2
    assert main(["run", "lister"]) == 0
    assert _states().count(ProductState.DRAFTED) == 1
    assert main(["run", "lister"]) == 3  # cap reached => exit code 3
    with session_scope() as s:
        last = s.execute(select(Run).order_by(Run.id.desc())).scalars().first()
        assert last.status == "cap_reached" and "DAILY_DRAFT_CAP" in last.errors_json[0]


def test_spend_cap_aborts_run(monkeypatch, brief_factory):
    monkeypatch.setenv("MAX_DAILY_SPEND_USD", "1")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    with session_scope() as s:
        s.add(Run(agent="creator", status="success", cost_usd=5.0))
        brief_factory(s)
    from pipeline.agents.creator import CreatorAgent

    outcome = CreatorAgent().run()
    assert outcome.status == "cap_reached" and outcome.exit_code == 3
    assert _states() == [ProductState.BRIEF]


def test_qa_rejects_blocklisted_brief(brief_factory):
    from pipeline.__main__ import main

    with session_scope() as s:
        brief_factory(s, concept="Original concept: pikachu themed print")
    assert main(["run", "creator"]) == 0  # creator refuses: blocklist hit in plan (prompt echoes concept)
    assert _states() == [ProductState.BRIEF]
    with session_scope() as s:
        p = s.execute(select(Product)).scalar_one()
        p.brief_json = {**p.brief_json, "concept": "Original concept: fern"}
    assert main(["run", "creator"]) == 0
    with session_scope() as s:
        p = s.execute(select(Product)).scalar_one()
        p.brief_json = {**p.brief_json, "concept": "mickey mouse print"}  # sneak IP in after creation
    assert main(["run", "qa"]) == 0
    with session_scope() as s:
        p = s.execute(select(Product)).scalar_one()
        assert p.state == ProductState.REJECTED and "blocklist" in p.qa_result_json["reasons"][0]


def test_status_and_smoke_commands(capsys):
    from pipeline.__main__ import main

    assert main(["status"]) == 0
    assert "Products by state" in capsys.readouterr().out
    assert main(["smoke", "--json"]) == 0
    assert '"ok": true' in capsys.readouterr().out


def test_dry_run_skips_etsy_writes(monkeypatch, brief_factory):
    from pipeline.__main__ import main

    with session_scope() as s:
        brief_factory(s)
    assert main(["run", "creator"]) == 0 and main(["run", "qa"]) == 0
    assert main(["--dry-run", "run", "lister"]) == 0
    with session_scope() as s:
        p = s.execute(select(Product)).scalar_one()
        assert p.state == ProductState.DRAFTED and p.etsy_listing_id is None and p.listing_json["dry_run"] is True
