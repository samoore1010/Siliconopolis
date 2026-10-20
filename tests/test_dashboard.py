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
    assert labels == ["Ideas", "Built", "With the Editor", "Approved", "Etsy drafts", "Live", "Rejected"]
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
    run = research["runs"][0]
    assert run["brief"] is None and run["ideas"] and run["markets"][0]["analysis"]["summary"]
    assert research["next_run"] and research["types"]
    assert isinstance(ov["needs_you"], list) and [s["key"] for s in ov["schedule"]][0] == "researcher"
    assert client.get("/api/dashboard/product/9999").status_code == 404
    assert client.get(f"/api/dashboard/file/{built[0]['id']}/99").status_code == 404


def test_design_html_is_never_served_as_html(client):
    _run("researcher", "creator")
    planners = [c for c in client.get("/api/dashboard/products").json()["products"]
                if c["product_type"] in ("planner", "tracker") and c["thumb"]]
    assert planners
    r = client.get(f"/api/dashboard/file/{planners[0]['id']}/design")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")


def test_limits_can_be_reset_and_spend_page_totals(client, session):
    from datetime import UTC, datetime, timedelta

    from pipeline.db.models import Product, Run
    from pipeline.guardrails.caps import briefs_this_week, todays_spend

    hdr = {"X-Siliconopolis": "1"}
    now = datetime.now(UTC)
    session.add(Run(agent="creator", status="success", cost_usd=4.0, started_at=now - timedelta(hours=2), ended_at=now,
                    items_processed=2, usage_json={"llm_calls": 3, "input_tokens": 1000, "output_tokens": 500, "images": 1}))
    session.add(Run(agent="qa", status="success", cost_usd=1.0, started_at=now - timedelta(days=40), ended_at=now, items_processed=1))
    for i in range(3):
        session.add(Product(state="brief", niche="n", product_type="planner", brief_json={"concept": f"c{i}"}, cost_usd=1.5 - i * 0.5))
    session.commit()
    ov = client.get("/api/dashboard/overview").json()
    assert ov["spend"]["today"] == 4.0 and ov["limits"]["ideas_this_week"] == 3 and ov["limits"]["spend_reset_at"] is None

    assert client.post("/api/dashboard/limits/spend/reset").status_code == 403  # CSRF header
    assert client.post("/api/dashboard/limits/nope/reset", headers=hdr).status_code == 404
    r = client.post("/api/dashboard/limits/spend/reset", headers=hdr).json()
    assert r["spend_today"] == 0.0
    assert todays_spend(session) == 0.0  # the agents' cap check uses the same function
    ov = client.get("/api/dashboard/overview").json()
    assert ov["spend"]["today"] == 0.0 and ov["limits"]["spend_reset_at"] and ov["limits"]["ideas_this_week"] == 3
    assert client.post("/api/dashboard/limits/ideas/reset", headers=hdr).json()["ideas_this_week"] == 0
    assert briefs_this_week(session) == 0
    session.add(Product(state="brief", niche="n", product_type="planner", brief_json={"concept": "after"}))
    session.commit()
    assert briefs_this_week(session) == 1  # new ideas count again from the reset
    assert any("reset" in a["text"] for a in client.get("/api/dashboard/overview").json()["activity"])

    sp = client.get("/api/dashboard/spend").json()
    assert sp["totals"]["all_time"] == 5.0 and sp["totals"]["last_7_days"] == 4.0 and sp["totals"]["today"] == 0.0
    assert sp["by_agent"][0]["agent"] == "creator" and sp["by_agent"][0]["images"] == 1
    assert sp["usage"]["input_tokens"] == 1000 and len(sp["by_month"]) >= 1 and sp["by_day"]
    assert [p["cost_usd"] for p in sp["top_products"]] == [1.5, 1.0, 0.5]
