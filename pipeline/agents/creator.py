"""Creator: brief -> product files + listing images (state=created)."""

from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from pipeline.adapters.images import get_image_provider
from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState, ProductType
from pipeline.generators import mockups
from pipeline.generators.pdf import image_to_pdf, render_planner_pdf
from pipeline.generators.xlsx import render_spreadsheet
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.storage import get_storage

from .base import Agent

PRINT_RATIOS = {"4x5 (8x10, 16x20)": (4, 5), "3x4 (9x12, 18x24)": (3, 4), "2x3 (4x6, 12x18, 24x36)": (2, 3),
                "ISO A (A4, A3, A2)": (210, 297), "11x14": (11, 14)}


class CreatorAgent(Agent):
    name = "creator"
    prompt_file = "creator"

    def __init__(self, *a, batch_size: int = 10, **kw):
        super().__init__(*a, **kw)
        self.batch_size = batch_size

    def execute(self, session: Session) -> None:
        owner = f"creator:{self.run_id}"
        products = claim_products(session, state=ProductState.BRIEF, owner=owner, limit=self.batch_size,
                                  lock_timeout_minutes=self.settings.lock_timeout_minutes)
        self.log.info("claimed briefs", count=len(products))
        storage = get_storage()
        blocklist = get_blocklist()
        images = get_image_provider()

        for product in products:
            try:
                self._create_one(session, product, storage, blocklist, images)
                transition(session, product, ProductState.CREATED)
                self.items_processed += 1
            except CapReached:
                release(session, product)
                session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                self.note_error(f"creator failed for product {product.id}: {e}", product_id=product.id)
                release(session, product)  # stays in brief for the next run
            session.commit()

    def _create_one(self, session, product: Product, storage, blocklist, images) -> None:
        brief = product.brief_json
        mock_hint = " (This is a MOCK run; placeholder art is expected.)" if self.settings.mode == "mock" else ""
        user = f"Brief:\n{json.dumps({**brief, 'product_type': product.product_type}, indent=2)}{mock_hint}"
        result = self.ask(session, task="creator_plan", model=self.settings.model_worker, user=user)
        plan = result.json()
        product.add_cost(result.cost_usd)

        hits = blocklist.check_many(plan.get("image_prompt"), plan.get("title_hint"), json.dumps(plan.get("planner_spec") or {}),
                                    json.dumps(plan.get("spreadsheet_spec") or {}))
        if hits:
            raise RuntimeError(f"blocklist hit in creator plan: {hits}")

        slug = re.sub(r"[^a-z0-9-]+", "-", str(plan.get("slug") or f"product-{product.id}").lower()).strip("-")[:40]
        base = f"products/{product.id}-{slug}"
        palette = plan.get("palette") or ["#F7F3EC", "#2B2B2B", "#A3B18A"]
        files: list[str] = []
        imgs: list[str] = []

        if product.product_type == ProductType.WALL_ART:
            gen = images.generate(plan["image_prompt"], size=(1024, 1280), negative_prompt=plan.get("negative_prompt"))
            product.add_cost(gen.cost_usd)
            self.record_cost(session, gen.cost_usd, kind="image")
            art = mockups.load(gen.data)
            files.append(storage.put(f"{base}/source.png", gen.data, "image/png"))
            for label, ratio in PRINT_RATIOS.items():
                fname = label.split(" ")[0].replace("x", "x")
                png = mockups.upscale_for_print(art, ratio=ratio)
                files.append(storage.put(f"{base}/print-{fname}.png", png, "image/png"))
            files.append(storage.put(f"{base}/print-letter.pdf", image_to_pdf(gen.data), "application/pdf"))
            sizes = list(PRINT_RATIOS)
        elif product.product_type in (ProductType.PLANNER, ProductType.TRACKER):
            spec = plan.get("planner_spec") or {"title": plan.get("title_hint", "Planner"), "sections": ["Priorities", "Schedule", "Notes"], "pages": 1}
            for page in ("letter", "a4"):
                pdf = render_planner_pdf(spec, page=page, palette=palette, kind=product.product_type)
                files.append(storage.put(f"{base}/{slug}-{page}.pdf", pdf, "application/pdf"))
            art = mockups.document_preview(spec.get("title", "Planner"), spec.get("sections", []), palette=palette)
            sizes = ["US Letter 8.5x11 in", "A4 210x297 mm", f"{spec.get('pages', 1)} page(s), PDF"]
        elif product.product_type == ProductType.SPREADSHEET:
            spec = plan.get("spreadsheet_spec") or {"title": plan.get("title_hint", "Template"), "sheets": [{"name": "Sheet1", "columns": ["Date", "Item", "Amount"]}]}
            xlsx = render_spreadsheet(spec, palette=palette)
            files.append(storage.put(f"{base}/{slug}.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
            art = mockups.document_preview(spec.get("title", "Template"), [s.get("name", "") for s in spec.get("sheets", [])], palette=palette)
            sizes = ["XLSX (Excel, Google Sheets, Numbers)", f"{len(spec.get('sheets', []))} sheet(s)"]
        else:
            raise RuntimeError(f"product_type {product.product_type} is Phase 2 (POD) and not implemented in v1")

        # 3-5 listing images: primary, framed mockup, flat lay, size chart
        imgs.append(storage.put(f"{base}/listing-01-primary.png", mockups.primary_image(art), "image/png"))
        imgs.append(storage.put(f"{base}/listing-02-framed.png", mockups.framed_mockup(art), "image/png"))
        imgs.append(storage.put(f"{base}/listing-03-flatlay.png", mockups.flat_lay(art), "image/png"))
        imgs.append(storage.put(f"{base}/listing-04-sizes.png", mockups.size_chart(sizes), "image/png"))

        product.file_paths = files
        product.image_paths = imgs
        product.brief_json = {**brief, "creator_plan": plan}
        self.log.info("product created", product_id=product.id, files=len(files), images=len(imgs))
