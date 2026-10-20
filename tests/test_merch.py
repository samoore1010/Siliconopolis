"""In-house apparel/mugs: catalog, print files, mockups, physical listings, orders and the print queue."""

from __future__ import annotations

import io

from PIL import Image

HDR = {"X-Siliconopolis": "1"}


def _client(auth=None):
    from fastapi.testclient import TestClient

    from pipeline.web.app import app

    client = TestClient(app)
    if auth:
        client.auth = auth
    return client


def _make(session, product_type="apparel", item="tshirt"):
    from pipeline.db.models import Product

    p = Product(state="brief", niche="custom occasion apparel", product_type=product_type,
                brief_json={"concept": "Personalized family reunion tee", "item": item, "target_price_usd": 26})
    session.add(p)
    session.commit()
    return p


def _built(session, product_type="apparel", item="tshirt"):
    from pipeline.agents.creator import CreatorAgent
    from pipeline.agents.qa import QAAgent
    from pipeline.db.models import Product

    p = _make(session, product_type, item)
    CreatorAgent().run()
    QAAgent().run()
    session.expire_all()
    return session.get(Product, p.id)


def test_catalog_loads():
    from pipeline.catalog import get_catalog

    c = get_catalog()
    tee = c.get("tshirt")
    assert tee.print_px == (3300, 3900) and tee.price_for("2XL") == tee.price + 3
    assert {i.key for i in c.for_type("mug")} == {"mug11", "mug15"}


def test_print_file_is_transparent_full_res_and_fits_long_names():
    from pipeline.generators import render

    html = ("<div class='design' style='padding:.4in'><div data-fit style='width:100%;font-size:160px'>{{name}}</div>"
            "</div>")
    ok = render.render_print_file(html, width_in=11, height_in=13, values={"name": "Henderson"})
    img = Image.open(io.BytesIO(ok.png))
    assert (img.width, img.height) == (3300, 3900) and img.mode == "RGBA" and ok.ok
    long = render.render_print_file(html, width_in=11, height_in=13, dpi=40, values={"name": "Okonkwo-Fitzgerald"})
    assert long.ok  # shrunk to fit
    huge = render.render_print_file(html, width_in=11, height_in=13, dpi=40, values={"name": "W" * 120})
    assert any("does not fit" in p for p in huge.problems)
    solid = render.render_print_file("<div class='design' style='background:#fff;color:#000'>Hi</div>",
                                     width_in=4, height_in=4, dpi=40)
    assert any("transparent" in p for p in solid.problems)


def test_creator_builds_apparel_with_print_file_and_mockups(session):
    from pipeline.db.models import ProductState

    p = _built(session)
    assert p.state == ProductState.APPROVED
    plan = p.brief_json["creator_plan"]
    assert plan["merch"]["item"] == "tshirt" and plan["merch"]["template"].endswith("design.html")
    assert plan["merch_spec"]["personalization"]["enabled"]
    assert p.file_paths[0].endswith("-example.png")
    names = [i.rsplit("/", 1)[-1] for i in p.image_paths]
    assert names[0] == "listing-01-primary.png" and "listing-03-colours.png" in names
    assert "listing-05-personalise.png" in names
    assert p.qa_result_json["file_check"]["ok"] and p.qa_result_json["file_check"]["size_px"] == [3300, 3900]


def test_physical_listing_waits_for_shipping_profile_then_drafts(session, monkeypatch):
    from pipeline.agents.lister import ListerAgent
    from pipeline.config import reset_settings_cache
    from pipeline.db.models import ProductState

    p = _built(session)
    ListerAgent().run()
    session.expire_all()
    assert session.get(type(p), p.id).state == ProductState.APPROVED  # no shipping profile yet

    monkeypatch.setenv("ETSY_SHIPPING_PROFILE_ID", "555")
    monkeypatch.setenv("ETSY_PRODUCTION_PARTNER_ID", "77")
    reset_settings_cache()
    ListerAgent().run()
    session.expire_all()
    p = session.get(type(p), p.id)
    listing = p.listing_json
    assert p.state == ProductState.DRAFTED and listing["type"] == "physical"
    assert listing["shipping_profile_id"] == "555" and listing["production_partner_ids"] == ["77"]
    assert listing["is_personalizable"] and listing["when_made"] == "made_to_order"
    colors = p.brief_json["creator_plan"]["merch_spec"]["colors"]
    assert len(listing["inventory"]["products"]) == 6 * len(colors)
    xxl = next(x for x in listing["inventory"]["products"] if "2xl" in x["sku"])
    assert xxl["offerings"][0]["price"] == 29.0
    from pipeline.adapters.etsy import get_etsy_client

    assert not get_etsy_client()._state["files"].get(p.etsy_listing_id)  # nothing digital uploaded


def _drafted(session, monkeypatch):
    from pipeline.agents.lister import ListerAgent
    from pipeline.config import reset_settings_cache

    p = _built(session)
    monkeypatch.setenv("ETSY_SHIPPING_PROFILE_ID", "555")
    reset_settings_cache()
    ListerAgent().run()
    session.expire_all()
    return session.get(type(p), p.id)


def test_orders_flow_to_print_queue_and_tracking_back_to_etsy(session, monkeypatch):
    from pipeline.adapters.etsy import get_etsy_client
    from pipeline.agents.fulfiller import FulfillerAgent
    from pipeline.db.models import FulfillmentOrder, OrderStatus

    p = _drafted(session, monkeypatch)
    etsy = get_etsy_client()
    etsy.add_mock_order(p.etsy_listing_id, variations={"Size": "L", "Color": "Navy"}, personalization="Henderson / 1987")
    etsy.add_mock_order(p.etsy_listing_id, variations={"Size": "XL", "Color": "Black"},
                        personalization="the Okonkwo-Fitzgerald family reunion 2026")
    FulfillerAgent().run()
    FulfillerAgent().run()  # idempotent
    session.expire_all()
    good, bad = session.query(FulfillmentOrder).order_by(FulfillmentOrder.id).all()
    assert session.query(FulfillmentOrder).count() == 2
    assert good.status == OrderStatus.READY and good.personalization == {"name": "Henderson", "year": "1987"}
    assert good.size == "L" and good.color == "Navy" and good.unit_cost == 9.5
    assert bad.status == OrderStatus.NEEDS_REVIEW and "14" in bad.review_reason

    monkeypatch.setenv("PRINT_USER", "mom")
    monkeypatch.setenv("PRINT_PASSWORD", "ink")
    monkeypatch.setenv("VILLAGE_USER", "steven")
    monkeypatch.setenv("VILLAGE_PASSWORD", "pw")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    mom = _client(("mom", "ink"))
    assert mom.get("/api/dashboard/overview").status_code == 401  # the shop login only sees the queue
    data = mom.get("/api/print/orders").json()
    assert data["role"] == "printer" and len(data["orders"]) == 2
    f = mom.get(f"/api/print/file/{good.id}/print")
    assert f.status_code == 200 and Image.open(io.BytesIO(f.content)).size == (3300, 3900)

    # fix the long name, then print and ship
    r = mom.post(f"/api/print/orders/{bad.id}/personalization", headers=HDR,
                 json={"values": {"name": "Okonkwo", "year": "2026"}}).json()
    assert r["problems"] == [] and r["order"]["status"] == "ready"
    assert mom.post(f"/api/print/orders/{good.id}/status", json={"status": "printing"}).status_code == 403  # header
    assert mom.post(f"/api/print/orders/{good.id}/status", headers=HDR, json={"status": "shipped"}).status_code == 422
    for o in (good, bad):
        assert mom.post(f"/api/print/orders/{o.id}/status", headers=HDR,
                        json={"status": "shipped", "carrier": "usps", "tracking_code": f"9400{o.id}"}).status_code == 200
    assert mom.post("/api/print/ledger/paid", headers=HDR).status_code == 403  # owner only
    owner = _client(("steven", "pw"))
    assert owner.post("/api/print/ledger/paid", headers=HDR).json()["paid"] == 19.0

    FulfillerAgent().run()
    session.expire_all()
    assert all(o.etsy_synced for o in session.query(FulfillmentOrder))
    shipped = [o for o in get_etsy_client()._state["orders"] if o.get("shipped")]
    assert len(shipped) == 2 and shipped[0]["tracking_code"].startswith("9400")


def test_mug_product(session):
    from pipeline.db.models import ProductState

    p = _built(session, "mug", "mug11")
    assert p.state == ProductState.APPROVED
    assert p.qa_result_json["file_check"]["size_px"] == [2550, 1050]


def test_physical_products_use_their_own_prompts_and_show_details(session):
    """Shirts/mugs are briefed with the physical prompt and the dashboard shows blank, colours, sizes and margin."""
    from fastapi.testclient import TestClient

    from pipeline.agents.creator import CreatorAgent
    from pipeline.agents.editor import EditorAgent
    from pipeline.db.models import Product
    from pipeline.prompts import load_prompt
    from pipeline.web.app import app

    assert CreatorAgent.prompt_for("mug") == "creator_physical" and CreatorAgent.prompt_for("planner") == "creator_digital"
    assert EditorAgent.prompt_for("apparel") == "editor_physical" and EditorAgent.prompt_for("wall_art") == "editor_digital"
    assert "PHYSICAL" in load_prompt("creator_physical") and "merch_spec" in load_prompt("creator_physical")
    assert "merch_spec" not in load_prompt("creator_digital") and "planner_spec" in load_prompt("creator_digital")
    assert "print area" in load_prompt("editor_physical") and "DIGITAL" in load_prompt("editor_digital")

    p = Product(state="brief", niche="n", product_type="mug", brief_json={"concept": "Family reunion mug", "target_price_usd": 19})
    session.add(p)
    session.commit()
    CreatorAgent().run()
    client = TestClient(app)
    d = client.get(f"/api/dashboard/product/{p.id}").json()
    det = d["physical_details"]
    assert det["item"] == "mug11" and det["material"].startswith("Glossy") and det["method"] == "Sublimation"
    assert det["sizes"] == [{"size": "11 oz", "price": 19.0}] and det["colors"][0]["name"] == "White"
    assert det["margin"] == 13.0 and [f["label"] for f in det["personalization"]] == ["Family name", "Year"]
    card = next(c for c in client.get("/api/dashboard/products").json()["products"] if c["id"] == p.id)
    assert card["physical"] and card["merch"]["label"] == "11 oz Ceramic Mug" and card["merch"]["personalized"]
    planner = next((c for c in client.get("/api/dashboard/products?state=brief").json()["products"]), None)
    assert planner is None or not planner["physical"]
