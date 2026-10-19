"""Owner dashboard: plain-English overview, products with reasons, market analysis, safe downloads."""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from pipeline.web.app import app

    return TestClient(app)


def _run(*agents):
    from pipeline.__main__ import main

    for a in agents:
        main(["run", a])


def test_research_saves_market_analysis_linked_to_ideas(session):
    import json

    from sqlalchemy import select

    from pipeline.db.models import Product, ResearchNote

    _run("researcher")
    notes = session.execute(select(ResearchNote).where(ResearchNote.source == "market")).scalars().all()
    assert notes, "researcher should store a market analysis per niche"
    data = json.loads(notes[0].content)
    assert data["analysis"]["summary"] and "keywords" in data
    p = session.execute(select(Product)).scalars().first()
    assert p.brief_json["market"]["note_id"] in {n.id for n in notes}


def test_dashboard_end_to_end(client):
    _run("researcher", "creator", "qa")
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Siliconopolis" in page.text

    ov = client.get("/api/dashboard/overview").json()
    labels = [s["label"] for s in ov["stages"]]
    assert labels == ["Ideas", "Built", "Approved", "Etsy drafts", "Live", "Rejected"]
    assert any("QA approved" in a["text"] or "QA rejected" in a["text"] for a in ov["activity"])
    assert not any(a["kind"] == "run_started" for a in ov["activity"])
    assert set(ov["last_runs"]) >= {"researcher", "creator", "qa"}

    cards = client.get("/api/dashboard/products").json()["products"]
    built = [c for c in cards if c["thumb"]]
    assert built and all(c["stage"] for c in cards)
    assert client.get("/api/dashboard/products?state=brief").json()["products"] == [c for c in cards if c["state"] == "brief"]

    d = client.get(f"/api/dashboard/product/{built[0]['id']}").json()
    assert d["why"]["rationale"] and d["market"]["analysis"]["summary"]
    assert d["qa"] is not None and d["images"] and d["files"] and d["timeline"]
    f = client.get(d["files"][0]["url"])
    assert f.status_code == 200 and f.content

    research = client.get("/api/dashboard/research").json()
    assert research["market"] and research["market"][0]["ideas_in_niche"] >= 1
    assert client.get("/api/dashboard/product/9999").status_code == 404
    assert client.get(f"/api/dashboard/file/{built[0]['id']}/99").status_code == 404


def test_design_html_is_never_served_as_html(client):
    _run("researcher", "creator")
    planners = [c for c in client.get("/api/dashboard/products").json()["products"]
                if c["product_type"] in ("planner", "tracker") and c["thumb"]]
    assert planners
    r = client.get(f"/api/dashboard/file/{planners[0]['id']}/design")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
