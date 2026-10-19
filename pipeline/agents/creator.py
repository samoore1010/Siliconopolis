"""Creator: brief -> product files + listing images (state=created).

- wall_art: image from the configured image provider (OpenAI in production), print-ratio files.
- planner/tracker: Claude designs every page as HTML/CSS, Chromium renders Letter + A4 PDFs; layout
  problems (overflow, clipped text) are sent back to Claude to fix before anything reaches QA.
- spreadsheet: typed columns + formulas in XLSX, preview rendered from the same spec.
Listing images use the real product (rendered pages), plus AI lifestyle scenes when available.
"""

from __future__ import annotations

import hashlib
import json
import re

from PIL import Image
from sqlalchemy.orm import Session

from pipeline.adapters.images import get_image_provider
from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState, ProductType
from pipeline.generators import mockups, render
from pipeline.generators.pdf import image_to_pdf
from pipeline.generators.xlsx import preview_html, render_spreadsheet
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.llm import LLMError
from pipeline.storage import get_storage

from .base import Agent

PRINT_RATIOS = {"4x5 (8x10, 16x20)": (4, 5), "3x4 (9x12, 18x24)": (3, 4), "2x3 (4x6, 12x18, 24x36)": (2, 3),
                "ISO A (A4, A3, A2)": (210, 297), "11x14": (11, 14)}
MAX_LAYOUT_FIXES = 2
MAX_CREATE_ATTEMPTS = 2  # after this many failed creations a brief is rejected instead of retried forever


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
                attempts = int((product.brief_json or {}).get("create_attempts", 0)) + 1
                product.brief_json = {**(product.brief_json or {}), "create_attempts": attempts, "last_create_error": str(e)[:500]}
                self.note_error(f"creator failed for product {product.id} (attempt {attempts}): {e}", product_id=product.id)
                if attempts >= MAX_CREATE_ATTEMPTS:
                    product.qa_result_json = {"pass": False, "reasons": [f"creation failed {attempts}x: {str(e)[:300]}"]}
                    transition(session, product, ProductState.REJECTED, message=f"creation failed {attempts}x")
                else:
                    release(session, product)  # stays in brief for the next run
            session.commit()

    # ------------------------------------------------------------------ helpers
    @property
    def _is_mock_run(self) -> bool:
        return self.settings.mode == "mock" and self.settings.app_env == "local"

    def _scene(self, session, storage, images, kind: str, key: int, product: Product) -> bytes | None:
        """Cached AI lifestyle background (generated once per variant, then reused across products)."""
        if not (images.is_real and self.settings.mockup_scenes):
            return None
        variants = mockups.SCENE_PROMPTS[kind]
        idx = key % len(variants)
        path = f"scenes/{kind}-{idx}.png"
        if storage.exists(path):
            return storage.get(path)
        try:
            size = (1536, 1024) if kind == "wall" else (1024, 1024)
            gen = images.generate(variants[idx], size=size, negative_prompt=mockups.SCENE_NEGATIVE,
                                  quality=self.settings.mockup_quality)
        except Exception as e:  # noqa: BLE001 - a missing scene must never block a product
            self.log.warning("scene generation failed; using styled fallback", kind=kind, error=str(e)[:200])
            return None
        product.add_cost(gen.cost_usd)
        self.record_cost(session, gen.cost_usd, kind="image")
        storage.put(path, gen.data, "image/png")
        return gen.data

    def _design_pages(self, session, product: Product, brief: dict, plan: dict, palette: list[str],
                      landscape: bool) -> str:
        spec = plan.get("planner_spec") or {}
        user = (f"Design brief:\n{json.dumps(spec, indent=2)}\n\nPalette (background, ink, accent): {palette}\n"
                f"Audience: {brief.get('audience')}\nStyle: {brief.get('style')}\n"
                f"Orientation: {'landscape' if landscape else 'portrait'}\nProduct title: {plan.get('title_hint')}")
        result = self.ask(session, task="creator_page_html", model=self.settings.model_worker, user=user,
                          max_tokens=16000, prompt="creator_page")
        product.add_cost(result.cost_usd)
        html = render.extract_html(result.text)
        for attempt in range(MAX_LAYOUT_FIXES + 1):
            problems = []
            for size in ("letter", "a4"):
                report = render.check_layout(html, size, landscape)
                problems += [f"[{size}] {p}" for p in report.problems]
            if not problems:
                return html
            self.log.info("layout problems found; asking for a fix", product_id=product.id, attempt=attempt + 1,
                          problems=problems[:5])
            if attempt == MAX_LAYOUT_FIXES:
                break
            fix = self.ask(session, task="creator_page_fix", model=self.settings.model_worker, max_tokens=16000,
                           prompt="creator_page",
                           user=(f"{user}\n\nYour previous HTML rendered with these layout problems:\n- "
                                 + "\n- ".join(problems[:12])
                                 + "\n\nFix every problem (reduce sizes, wrap text, use fr units) without removing "
                                   f"required elements. Return the full corrected document.\n\n```html\n{html}\n```"))
            product.add_cost(fix.cost_usd)
            html = render.extract_html(fix.text)
        raise RuntimeError(f"page layout still broken after {MAX_LAYOUT_FIXES} fixes: {problems[:3]}")

    @staticmethod
    def _card(title: str, items: list[str], palette: list[str]) -> bytes:
        """'What's included' card rendered to fit its content, then framed at listing size."""
        png = render.html_to_png(mockups.included_html(title, items, palette=palette), width=1000, height=1000,
                                 full_page=True)
        return mockups.primary_image(mockups.load(png), bg=palette[0])

    # ------------------------------------------------------------------ main
    def _create_one(self, session, product: Product, storage, blocklist, images) -> None:
        brief = {k: v for k, v in (product.brief_json or {}).items() if k not in ("create_attempts", "last_create_error")}
        mock_hint = " (This is a MOCK run; placeholder art is expected.)" if self._is_mock_run else ""
        user = f"Brief:\n{json.dumps({**brief, 'product_type': product.product_type}, indent=2)}{mock_hint}"
        result = self.ask(session, task="creator_plan", model=self.settings.model_worker, user=user,
                          max_tokens=16000)
        try:
            plan = result.json()
        except LLMError as e:
            raise RuntimeError(f"creator plan was not valid JSON: {e}") from e
        if isinstance(plan, list):  # model occasionally wraps the object in a list
            plan = next((p for p in plan if isinstance(p, dict)), None)
        if not isinstance(plan, dict):
            raise RuntimeError("creator plan JSON was not an object")
        product.add_cost(result.cost_usd)

        hits = blocklist.check_many(plan.get("image_prompt"), plan.get("title_hint"), json.dumps(plan.get("planner_spec") or {}),
                                    json.dumps(plan.get("spreadsheet_spec") or {}))
        if hits:
            raise RuntimeError(f"blocklist hit in creator plan: {hits}")

        slug = re.sub(r"[^a-z0-9-]+", "-", str(plan.get("slug") or f"product-{product.id}").lower()).strip("-")[:40]
        base = f"products/{product.id}-{slug}"
        palette = (plan.get("palette") or []) + ["#F7F3EC", "#2B2B2B", "#A3B18A"]
        palette = palette[:3]
        scene_key = int(hashlib.md5(slug.encode()).hexdigest(), 16)
        title = mockups.short_title(str(plan.get("title_hint") or brief.get("concept", "Printable")))
        files: list[str] = []
        imgs: list[str] = []

        if product.product_type == ProductType.WALL_ART:
            if not images.is_real and not self._is_mock_run:
                raise RuntimeError("wall art needs a real IMAGE_PROVIDER (e.g. openai); mock images are not sellable")
            gen = images.generate(plan["image_prompt"], size=(1024, 1536), negative_prompt=plan.get("negative_prompt"))
            product.add_cost(gen.cost_usd)
            self.record_cost(session, gen.cost_usd, kind="image")
            art = mockups.load(gen.data)
            # Etsy allows 5 digital files per listing: deliver the 5 print ratios; keep sources aside.
            source = storage.put(f"{base}/source/original.png", gen.data, "image/png")
            storage.put(f"{base}/source/print-letter.pdf", image_to_pdf(gen.data), "application/pdf")
            plan = {**plan, "source_image": source}
            for label, ratio in PRINT_RATIOS.items():
                fname = label.split(" ")[0]
                files.append(storage.put(f"{base}/print-{fname}.png", mockups.upscale_for_print(art, ratio=ratio), "image/png"))
            scene = self._scene(session, storage, images, "wall", scene_key, product)
            imgs.append(storage.put(f"{base}/listing-01-primary.png", mockups.primary_image(art), "image/png"))
            imgs.append(storage.put(f"{base}/listing-02-room.png",
                                    mockups.scene_composite(art, scene, kind="wall") if scene else mockups.framed_mockup(art), "image/png"))
            imgs.append(storage.put(f"{base}/listing-03-detail.png", mockups.detail_crop(art), "image/png"))
            sizes = list(PRINT_RATIOS)
            imgs.append(storage.put(f"{base}/listing-04-included.png", self._card(
                title, [f"Print ratio {s}" for s in sizes] + ["High-resolution PNG files"], palette), "image/png"))

        elif product.product_type in (ProductType.PLANNER, ProductType.TRACKER):
            spec = plan.get("planner_spec") or {}
            landscape = str(spec.get("orientation", "portrait")).lower() == "landscape"
            html = self._design_pages(session, product, brief, plan, palette, landscape)
            hits = blocklist.check_many(re.sub(r"<[^>]+>", " ", html))
            if hits:
                raise RuntimeError(f"blocklist hit in page text: {hits}")
            design_path = storage.put(f"{base}/source/design.html", html.encode("utf-8"), "text/html")
            plan = {**plan, "design_html": design_path}  # kept for re-renders; never uploaded to Etsy
            for size in ("letter", "a4"):
                pdf = render.html_to_pdf(html, size, landscape)
                files.append(storage.put(f"{base}/{slug}-{size}.pdf", pdf, "application/pdf"))
            pages = [mockups.load(p) for p in render.html_to_page_pngs(html, "letter", landscape=landscape)]
            if not pages:
                raise RuntimeError("rendered planner has no pages")
            scene = self._scene(session, storage, images, "desk", scene_key, product)
            imgs.append(storage.put(f"{base}/listing-01-primary.png", mockups.page_hero(pages[0], bg=palette[0]), "image/png"))
            imgs.append(storage.put(f"{base}/listing-02-lifestyle.png",
                                    mockups.scene_composite(pages[0], scene) if scene
                                    else mockups.styled_flat_lay(pages[0], accent=palette[2]), "image/png"))
            imgs.append(storage.put(f"{base}/listing-03-pages.png",
                                    mockups.pages_spread(pages, bg=palette[0]) if len(pages) > 1
                                    else mockups.detail_crop(pages[0], bg=palette[0]), "image/png"))
            included = [f"{len(pages)} printable page{'s' if len(pages) > 1 else ''}"] + \
                       [str(p.get("name")) for p in (spec.get("pages") or []) if isinstance(p, dict) and p.get("name")][:4] + \
                       ["US Letter + A4 PDF", "Print at home or at a print shop"]
            imgs.append(storage.put(f"{base}/listing-04-included.png", self._card(title, included, palette), "image/png"))

        elif product.product_type == ProductType.SPREADSHEET:
            spec = plan.get("spreadsheet_spec") or {"title": title, "sheets": [{"name": "Sheet1", "columns": ["Date", "Item", "Amount"]}]}
            xlsx = render_spreadsheet(spec, palette=palette)
            files.append(storage.put(f"{base}/{slug}.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
            sheets = spec.get("sheets") or []

            def shot(idx: list[int]) -> Image.Image:
                return mockups.load(render.html_to_png(preview_html(spec, palette=palette, sheets=idx),
                                                       width=1100, height=700, full_page=True))

            preview = shot([0, 1])
            scene = self._scene(session, storage, images, "desk", scene_key, product)
            imgs.append(storage.put(f"{base}/listing-01-primary.png", mockups.primary_image(preview, bg=palette[0]), "image/png"))
            imgs.append(storage.put(f"{base}/listing-02-lifestyle.png",
                                    mockups.scene_composite(preview, scene, framed=True, rotate=0) if scene
                                    else mockups.styled_flat_lay(preview, accent=palette[2]), "image/png"))
            if len(sheets) > 2:  # show every remaining tab so buyers (and QA) see all features
                imgs.append(storage.put(f"{base}/listing-03-more-tabs.png",
                                        mockups.primary_image(shot(list(range(2, min(len(sheets), 4)))), bg=palette[0]),
                                        "image/png"))
            sheet_names = [str(s.get("name")) for s in sheets][:5]
            imgs.append(storage.put(f"{base}/listing-04-included.png", self._card(title, [
                f"{len(sheet_names)} tabs: " + ", ".join(sheet_names), "Formulas and totals built in",
                "Opens in Excel and Google Sheets", "Delivered blank (example data shown in photos)"], palette), "image/png"))
        else:
            raise RuntimeError(f"product_type {product.product_type} is Phase 2 (POD) and not implemented in v1")

        product.file_paths = files
        product.image_paths = imgs
        product.brief_json = {**brief, "creator_plan": plan}
        self.log.info("product created", product_id=product.id, files=len(files), images=len(imgs))
