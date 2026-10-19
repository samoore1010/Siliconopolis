"""Pipeline keeps working before Etsy approves the app; drafting starts once Etsy is connected."""

from __future__ import annotations

from conftest import make_brief


def _approve(session, n=2):
    from pipeline.db.models import ProductState

    out = []
    for i in range(n):
        p = make_brief(session, product_type="planner", concept=f"Original planner {i}")
        p.state = ProductState.APPROVED
        p.file_paths, p.image_paths = [], []
        out.append(p)
    session.commit()
    return [p.id for p in out]


def test_lister_waits_when_etsy_not_connected(session, monkeypatch):
    from pipeline.adapters.etsy.mock import MockEtsyClient
    from pipeline.agents.lister import ListerAgent
    from pipeline.db.models import Product, ProductState

    ids = _approve(session)
    monkeypatch.setattr(MockEtsyClient, "is_connected", lambda self: False)
    outcome = ListerAgent().run()
    assert outcome.ok
    assert outcome.items_processed == 0
    session.expire_all()
    for pid in ids:
        p = session.get(Product, pid)
        assert p.state == ProductState.APPROVED
        assert p.locked_by is None


def test_lister_drafts_once_connected(session):
    from pipeline.agents.lister import ListerAgent
    from pipeline.db.models import Product, ProductState

    ids = _approve(session)
    outcome = ListerAgent().run()
    assert outcome.items_processed == len(ids)
    session.expire_all()
    assert all(session.get(Product, pid).state == ProductState.DRAFTED for pid in ids)


def test_live_client_not_connected_without_tokens():
    from pipeline.adapters.etsy.live import LiveEtsyClient

    c = LiveEtsyClient(api_key="k", shop_id="1", dry_run=False)
    assert c.is_connected() is False


def test_researcher_holds_wall_art_without_image_provider(monkeypatch):
    from pipeline.agents.researcher import ResearcherAgent
    from pipeline.config import get_settings

    ag = ResearcherAgent()
    monkeypatch.setattr(ag, "settings", get_settings().model_copy(update={"app_env": "production"}))
    seen = []
    real_ask = ag.ask

    def spy(session, *, task, model, user, **kw):
        seen.append(user)
        return real_ask(session, task=task, model=model, user=user, **kw)

    monkeypatch.setattr(ag, "ask", spy)
    ag.run()
    assert seen and all("wall_art" not in u.split("Allowed product_type values:")[1] for u in seen)
