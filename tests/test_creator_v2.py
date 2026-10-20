"""Creator v2: HTML-designed planners, typed spreadsheets, OpenAI images, cached scenes."""

from __future__ import annotations

import base64
import io

import pytest
from conftest import make_brief
from PIL import Image


def _png(w=64, h=64, color="#C9A57E") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_planner_is_designed_as_html_and_rendered(session):
    from pipeline.agents.creator import CreatorAgent
    from pipeline.db.models import Product, ProductState
    from pipeline.storage import get_storage

    p = make_brief(session, niche="planners", product_type="planner", concept="Daily focus planner")
    session.commit()
    outcome = CreatorAgent().run()
    assert outcome.items_processed == 1
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.CREATED
    storage = get_storage()
    pdfs = [f for f in p.file_paths if f.endswith(".pdf")]
    assert len(pdfs) == 2 and all(storage.get(f).startswith(b"%PDF") for f in pdfs)
    assert not any(f.endswith(".html") for f in p.file_paths), "design source must not be uploaded to Etsy"
    assert p.brief_json["creator_plan"]["design_html"].endswith("design.html")
    assert len(p.image_paths) == 4
    first = Image.open(io.BytesIO(storage.get(p.image_paths[0])))
    assert first.size == (2000, 2000)


def test_layout_check_flags_overflow():
    from pipeline.generators import render

    bad = "<div class='page'><p style='white-space:nowrap;font-size:40px'>" + "overflowing words " * 20 + "</p></div>"
    assert not render.check_layout(bad).ok
    good = "<div class='page' style='padding:.5in'><h1>Fine</h1></div>"
    assert render.check_layout(good).ok


def test_render_strips_scripts_and_blocks_network():
    from pipeline.generators import render

    html = ("<div class='page'><script>document.body.innerHTML='HACKED'</script>"
            "<img src='https://example.com/x.png' onerror=\"document.title='x'\"><h1>Safe</h1></div>")
    wrapped = render.wrap(html)
    assert "<script" not in wrapped and "onerror" not in wrapped
    assert render.html_to_pdf(html).startswith(b"%PDF")


def test_spreadsheet_preview_and_typed_columns(session):
    from pipeline.agents.creator import CreatorAgent
    from pipeline.db.models import Product, ProductState

    p = make_brief(session, niche="sheets", product_type="spreadsheet", concept="Bookkeeping workbook")
    session.commit()
    CreatorAgent().run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.CREATED and p.file_paths[0].endswith(".xlsx")
    # dashboard hero, lifestyle, tabs 1-2, remaining tabs (mock spec has 3), instructions, what's-included
    names = [i.rsplit("/", 1)[-1] for i in p.image_paths]
    assert "listing-04-more-tabs.png" in names and names[-1] == "listing-06-included.png"
    check = p.brief_json["creator_plan"]["file_check"]
    assert check["ok"] and check["formulas"] > 0
    assert check["sheets"][:2] == ["Start Here", "Dashboard"] and "Settings" in check["sheets"]


def test_wall_art_refused_with_mock_images_off_local(session, monkeypatch):
    from pipeline.agents.creator import CreatorAgent
    from pipeline.config import get_settings
    from pipeline.db.models import Product, ProductState

    p = make_brief(session)  # wall_art
    session.commit()
    agent = CreatorAgent()
    monkeypatch.setattr(agent, "settings", get_settings().model_copy(update={"app_env": "production"}))
    agent.run()
    agent2 = CreatorAgent()
    monkeypatch.setattr(agent2, "settings", get_settings().model_copy(update={"app_env": "production"}))
    agent2.run()
    session.expire_all()
    p = session.get(Product, p.id)
    assert p.state == ProductState.REJECTED  # gave up after MAX_CREATE_ATTEMPTS instead of retrying forever
    assert "real IMAGE_PROVIDER" in p.brief_json["last_create_error"]


def test_openai_provider_request_and_cost(monkeypatch):
    from pipeline.adapters.images.openai import OpenAIImageProvider, estimate_cost

    sent = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"data": [{"b64_json": base64.b64encode(_png()).decode()}]}

    def fake_post(path, json):
        sent.update(path=path, **json)
        return Resp()

    prov = OpenAIImageProvider(api_key="sk-test", model="gpt-image-1.5", quality="high")
    monkeypatch.setattr(prov._http, "post", fake_post)
    img = prov.generate("a fern", size=(1024, 1536), negative_prompt="text, logos")
    assert sent["path"] == "/images/generations" and sent["size"] == "1024x1536" and sent["quality"] == "high"
    assert "Avoid: text, logos" in sent["prompt"]
    assert img.data.startswith(b"\x89PNG") and img.cost_usd == estimate_cost("gpt-image-1.5", "high", "1024x1536") > 0


def test_openai_provider_requires_key():
    from pipeline.adapters.images.openai import OpenAIImageProvider

    with pytest.raises(RuntimeError, match="IMAGE_API_KEY"):
        OpenAIImageProvider(api_key="")


def test_scene_generated_once_and_cached(session, monkeypatch):
    from pipeline.adapters.images.base import GeneratedImage, ImageProvider
    from pipeline.agents.creator import CreatorAgent
    from pipeline.storage import get_storage

    calls = []

    class FakeReal(ImageProvider):
        name = "fake-real"

        def generate(self, prompt, *, size=(1024, 1024), style=None, negative_prompt=None, quality=None):
            calls.append(prompt)
            return GeneratedImage(data=_png(1024, 1024), media_type="image/png", width=1024, height=1024,
                                  cost_usd=0.05, model="fake")

        def ping(self):
            return None

    p = make_brief(session, product_type="planner")
    session.commit()
    agent = CreatorAgent()
    storage = get_storage()
    a = agent._scene(session, storage, FakeReal(), "desk", 0, p)
    b = agent._scene(session, storage, FakeReal(), "desk", 0, p)
    assert a == b and len(calls) == 1 and storage.exists("scenes/desk-0.png")


def test_preview_formats_and_short_titles():
    from pipeline.generators.mockups import short_title
    from pipeline.generators.xlsx import normalize_columns, preview_html

    assert short_title("Small Business Bookkeeping Spreadsheet | Income Expense Tracker, Profit") == \
        "Small Business Bookkeeping Spreadsheet"
    assert len(short_title("Mileage Log Spreadsheet Tracker with Monthly Summary and Totals Included")) <= 48
    cols = normalize_columns(["Paid?", "Deposit?", "Date Paid", "Outstanding", "Notes"])
    assert [c["type"] for c in cols] == ["checkbox", "checkbox", "date", "currency", "text"]
    spec = {"title": "Book | keyword stuffing", "sheets": [
        {"name": "A", "columns": ["Item"], "sample_rows": [["x"]]},
        {"name": "B", "columns": ["Item"]},
        {"name": "Summary", "columns": [{"name": "Profit Margin", "type": "formula", "formula": "=B{r}/A{r}"},
                                        {"name": "Net", "type": "formula", "formula": "=A{r}", "format": "currency"}],
         "sample_rows": [[0.55, 1200]]}]}
    html = preview_html(spec, sheets=[2])
    assert "55%" in html and "$1,200.00" in html and "keyword stuffing" not in html
    assert "Example entries included" in html


def test_extract_html_strips_stray_fences():
    from pipeline.generators import render

    doc = "<html><body><div class='page'>x</div></body></html>"
    assert render.extract_html(f"```html\n{doc}\n```") == doc
    assert render.extract_html(f"```html\n{doc}") == doc  # unterminated fence used to print "```html" on the page
    assert render.extract_html(f"Here is the design:\n{doc}\n```") == doc
