"""The Editor: owner-triggered polish of built/approved/rejected products, its own queue, Stop, budget,
back through QA which verifies the requested changes."""

from __future__ import annotations

import pytest

HDR = {"X-Siliconopolis": "1"}


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from pipeline.web.app import app

    return TestClient(app)


def _built(session, product_type="planner", concept="Daily focus planner", qa=True):
    from pipeline.agents.creator import CreatorAgent
    from pipeline.agents.qa import QAAgent
    from pipeline.db.models import Product

    p = Product(state="brief", niche="n", product_type=product_type, brief_json={"concept": concept, "target_price_usd": 8})
    session.add(p)
    session.commit()
    CreatorAgent().run()
    if qa:
        QAAgent().run()
    session.expire_all()
    return session.get(Product, p.id)


def test_send_to_editor_polish_and_qa_verifies(session, client, monkeypatch):
    from pipeline.agents.editor import EditorAgent
    from pipeline.agents.qa import QAAgent
    from pipeline.db.models import OwnerNote, Product, ProductState
    from pipeline.llm import MockLLM, get_llm
    from pipeline.storage import get_storage

    p = _built(session)
    assert p.state == ProductState.APPROVED
    p.qa_result_json = {**p.qa_result_json, "improvements": ["Give the habit grid more room"]}
    session.commit()
    before_files, before_images = list(p.file_paths), list(p.image_paths)

    assert client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "warmer"}).status_code == 403  # CSRF header
    r = client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "Make it warmer", "include_qa": True}, headers=HDR)
    assert r.status_code == 200 and r.json()["state"] == ProductState.EDITING
    assert r.json()["edit"]["improvements"] == ["Give the habit grid more room"] and r.json()["edit"]["previous_state"] == "approved"
    assert client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "again"}, headers=HDR).status_code == 409
    assert client.post(f"/api/dashboard/product/{p.id}/comments", json={"text": "x", "revise": True}, headers=HDR).status_code == 409
    d = client.get(f"/api/dashboard/product/{p.id}").json()
    assert d["edit_pending"]["note"] == "Make it warmer" and not d["can_edit"] and d["stage"] == "With the Editor"
    q = client.get("/api/dashboard/queue/editor").json()
    assert [x["id"] for x in q["queue"]] == [p.id] and q["batch_size"] == EditorAgent.DEFAULT_BATCH
    assert client.get("/api/dashboard/queue").json()["queue"] == []  # not in the Creator's queue
    ov = client.get("/api/dashboard/overview").json()
    assert any("sent" in a["text"] and "Editor" in a["text"] for a in ov["activity"])
    assert any(s["key"] == "editor" for s in ov["schedule"])

    outcome = EditorAgent().run()
    assert outcome.status == "success" and outcome.items_processed == 1 and not outcome.errors
    calls = [c for c in get_llm().calls if c["task"] == "editor_pages"]
    assert len(calls) == 1 and "Make it warmer" in calls[0]["user"] and "habit grid" in calls[0]["user"]
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["n_images"] >= 1
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.CREATED and "edit" not in p.brief_json and p.qa_result_json is None
    storage = get_storage()
    assert p.file_paths != before_files and "/v2/" in p.file_paths[0]
    assert b"Polished planner" in storage.get(p.brief_json["creator_plan"]["design_html"])
    assert storage.exists(storage.key_from_uri(before_images[0]))  # the version it started from is kept
    rec = p.brief_json["edits_done"][0]
    assert rec["previous_images"] == before_images and rec["changes_made"] and rec["model"] == "claude-opus-5-5"
    assert {n.kind: n.status for n in session.query(OwnerNote).filter_by(product_id=p.id)} == {"edit": "addressed"}

    # QA checks the requested changes were made; a skipped one fails the product
    orig = MockLLM._canned

    def skipped(task, user):
        out = orig(task, user)
        if task == "qa_vision":
            assert "POLISHED version" in user and "Make it warmer" in user
            out["requested_changes"][0]["done"] = False
        return out

    monkeypatch.setattr(MockLLM, "_canned", staticmethod(skipped))
    QAAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.REJECTED and any("requested changes not made" in r for r in p.qa_result_json["reasons"])
    monkeypatch.setattr(MockLLM, "_canned", staticmethod(orig))

    d = client.get(f"/api/dashboard/product/{p.id}").json()
    assert d["edits"][0]["before"] and d["can_edit"]
    assert client.get(d["edits"][0]["before"][0]).headers["content-type"] == "image/jpeg"
    # a second polish from the rejected state, then QA approves it
    assert client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "Actually make it warmer", "include_qa": True}, headers=HDR).status_code == 200
    EditorAgent().run()
    QAAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.APPROVED and len(p.brief_json["edits_done"]) == 2 and "/v3/" in p.file_paths[0]
    assert p.qa_result_json["requested_changes"] and all(c["done"] for c in p.qa_result_json["requested_changes"])
    # deleting it also removes the kept earlier versions
    assert client.delete(f"/api/dashboard/product/{p.id}", headers=HDR).status_code == 200
    assert not storage.list(f"products/{p.id}-")


def test_editor_refuses_etsy_products_and_unbuilt_ideas(session, client):
    from pipeline.db.models import Product

    for state in ("brief", "drafted", "published"):
        session.add(Product(state=state, niche="n", product_type="planner", brief_json={"concept": "c"}, file_paths=["x"]))
    session.commit()
    for p in session.query(Product):
        r = client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "polish"}, headers=HDR)
        assert r.status_code == 409, p.state
    p = Product(state="created", niche="n", product_type="planner", brief_json={"concept": "c"})
    session.add(p)
    session.commit()
    assert client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "polish"}, headers=HDR).status_code == 409
    # a note or QA's suggestions are needed: an empty request is refused
    built = _built(session, "spreadsheet", "Bookkeeping workbook")
    built.qa_result_json = {**built.qa_result_json, "improvements": []}
    session.commit()
    assert client.post(f"/api/dashboard/product/{built.id}/edit", json={"note": ""}, headers=HDR).status_code == 422


def test_polish_queue_order_take_back_and_stop(session, client, monkeypatch):
    from pipeline.agents.editor import EditorAgent
    from pipeline.db.models import Product, ProductState

    ids = [_built(session, "spreadsheet", f"Workbook {i}", qa=False).id for i in range(3)]
    a, b, c = ids
    for i in ids:
        assert client.post(f"/api/dashboard/product/{i}/edit", json={"note": "tidy"}, headers=HDR).status_code == 200
    q = client.get("/api/dashboard/queue/editor").json()
    assert [x["id"] for x in q["queue"]] == [c, b, a]  # the latest request is first
    assert client.post("/api/dashboard/queue/editor/order", headers=HDR, json={"ids": [a, b, c]}).json()["order"] == [a, b, c]
    assert client.post(f"/api/dashboard/queue/editor/{c}/next", headers=HDR).status_code == 200
    assert [x["id"] for x in client.get("/api/dashboard/queue/editor").json()["queue"]] == [c, a, b]
    # take one back: it returns to where it was, untouched
    assert client.delete(f"/api/dashboard/queue/editor/{b}", headers=HDR).json()["state"] == ProductState.CREATED
    session.expire_all()
    assert "edit" not in session.get(Product, b).brief_json
    assert client.delete(f"/api/dashboard/queue/editor/{b}", headers=HDR).status_code == 404

    assert client.post("/api/dashboard/agents/editor/stop", headers=HDR).status_code == 409  # not running
    original = EditorAgent.report_progress
    calls = []

    def progress_then_stop(self):
        original(self)
        calls.append(1)
        if len(calls) == 1:
            working = client.get("/api/dashboard/overview").json()["working"]
            assert working[0]["agent"] == "editor" and working[0]["stoppable"] and working[0]["planned"] == EditorAgent.DEFAULT_BATCH
            assert client.post("/api/dashboard/agents/editor/stop", headers=HDR).status_code == 200

    monkeypatch.setattr(EditorAgent, "report_progress", progress_then_stop)
    outcome = EditorAgent().run()
    session.expire_all()
    assert outcome.status == "stopped" and outcome.items_processed == 1
    assert session.get(Product, c).state == ProductState.CREATED and session.get(Product, a).state == ProductState.EDITING
    assert session.get(Product, a).locked_by is None


def test_editor_budget_hands_the_product_back(session, monkeypatch):
    from pipeline.agents.editor import EditorAgent
    from pipeline.db.models import Product, ProductState

    p = _built(session, "spreadsheet", "Bookkeeping workbook")
    p.brief_json = {**p.brief_json, "edit": {"note": "tidy", "improvements": [], "round": 1, "previous_state": "approved"}}
    p.state = ProductState.EDITING
    p.cost_usd = 10.0
    session.commit()
    monkeypatch.setenv("EDITOR_MAX_COST_PER_PRODUCT_USD", "1")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    agent = EditorAgent()
    original = agent._spend_check

    def over_budget(product):  # pretend this polish already spent more than the cap
        agent._budget_start = float(product.cost_usd) - 2.0
        original(product)

    monkeypatch.setattr(agent, "_spend_check", over_budget)
    outcome = agent.run()
    assert outcome.status == "success" and outcome.errors and "limit" in outcome.errors[0]
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.APPROVED and "edit" not in p.brief_json
    assert p.brief_json["edits_done"][0]["failed"] and "EDITOR_MAX_COST_PER_PRODUCT_USD" in p.brief_json["last_edit_error"]


def test_editor_failure_retries_once_then_gives_back(session, monkeypatch):
    from pipeline.agents.editor import EditorAgent
    from pipeline.db.models import Product, ProductState

    p = _built(session, "spreadsheet", "Bookkeeping workbook", qa=False)
    p.brief_json = {**p.brief_json, "edit": {"note": "tidy", "improvements": [], "round": 1, "previous_state": "created"}}
    p.state = ProductState.EDITING
    session.commit()
    monkeypatch.setattr(EditorAgent, "_edit_one", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    EditorAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.EDITING and p.brief_json["edit"]["attempts"] == 1 and p.locked_by is None
    EditorAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.CREATED and p.brief_json["edits_done"][0]["error"] == "boom"


def test_split_reply_parses_json_then_html():
    from pipeline.agents.editor import split_reply
    from pipeline.llm import LLMError

    meta, html = split_reply('Here:\n```json\n{"changes_made": ["a"]}\n```\n```html\n<div class="page">x</div>\n```')
    assert meta == {"changes_made": ["a"]} and html == '<div class="page">x</div>'
    meta, html = split_reply('{"spreadsheet_spec": {"title": "t"}}')
    assert meta["spreadsheet_spec"]["title"] == "t" and html is None
    with pytest.raises(LLMError):
        split_reply("no json here")


def test_creator_revision_that_cannot_build_is_not_stuck(session):
    """A pending rebuild that fails twice ends up rejected WITHOUT 'your changes are being made' forever."""
    from pipeline.agents.creator import CreatorAgent
    from pipeline.db.models import Product, ProductState

    p = Product(state="brief", niche="n", product_type="pod_poster", brief_json={
        "concept": "c", "revision": {"feedback": "- bigger", "round": 1, "previous_plan": {}}})
    session.add(p)
    session.commit()
    CreatorAgent().run()
    CreatorAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.REJECTED and "revision" not in p.brief_json
    assert p.brief_json["revisions_done"][0]["failed"]
