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
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.images import get_image_provider
from pipeline.catalog import get_catalog
from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import OwnerNote, OwnerNoteKind, Product, ProductState, ProductType
from pipeline.generators import merch, mockups, office, render
from pipeline.generators.pdf import image_to_pdf
from pipeline.generators.xlsx import (
    SpreadsheetError,
    preview_html,
    render_spreadsheet,
    sheet_snapshots,
    spec_problems,
    validate_xlsx,
    window_html,
)
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.llm import LLMError
from pipeline.storage import get_storage

from .base import Agent

PRINT_RATIOS = {"4x5 (8x10, 16x20)": (4, 5), "3x4 (9x12, 18x24)": (3, 4), "2x3 (4x6, 12x18, 24x36)": (2, 3),
                "ISO A (A4, A3, A2)": (210, 297), "11x14": (11, 14)}
MAX_LAYOUT_FIXES = 2
MAX_CREATE_ATTEMPTS = 2  # after this many failed creations a brief is rejected instead of retried forever
MAX_SHEET_FIXES = 2
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# brief keys that are bookkeeping, not part of the product idea
INTERNAL_KEYS = ("create_attempts", "last_create_error", "creator_plan", "revision", "revisions_done")


def revision_note(brief: dict) -> str:
    """Owner feedback for a rebuild, phrased for the Creator (and QA)."""
    rev = brief.get("revision") or {}
    if not rev.get("feedback"):
        return ""
    prev = rev.get("previous_plan") or {}
    prev_summary = {k: prev.get(k) for k in ("title_hint", "palette", "planner_spec", "spreadsheet_spec", "image_prompt")
                    if prev.get(k)}
    return ("\n\nOWNER REVISION REQUEST (round {round}) - this is the shop owner's direct feedback on the previous "
            "version. Address every point; it overrides the brief where they conflict:\n{fb}\n\n"
            "Previous version (change what the owner asked for, keep what they did not mention):\n{prev}").format(
        round=rev.get("round", 1), fb=rev["feedback"], prev=json.dumps(prev_summary)[:12000])


class CreatorAgent(Agent):
    name = "creator"
    prompt_file = "creator"

    DEFAULT_BATCH = 10  # ideas built per daily run

    def __init__(self, *a, batch_size: int = DEFAULT_BATCH, **kw):
        super().__init__(*a, **kw)
        self.batch_size = batch_size

    def execute(self, session: Session) -> None:
        """Build up to batch_size ideas, ONE claim at a time, so the owner can reorder the queue or
        press Stop while the run is going: the next item is always the current top of the queue."""
        owner = f"creator:{self.run_id}"
        storage = get_storage()
        blocklist = get_blocklist()
        images = get_image_provider()
        tried: set[int] = set()  # a failed build is retried next run, not again in this one
        for _ in range(self.batch_size):
            if self.should_stop():
                break
            claimed = claim_products(session, state=ProductState.BRIEF, owner=owner, limit=1,
                                     lock_timeout_minutes=self.settings.lock_timeout_minutes, exclude_ids=tried)
            if not claimed:
                break
            product = claimed[0]
            tried.add(product.id)
            self.log.info("building", product_id=product.id)
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
                    release(session, product)
                    product.priority = None  # a failed build goes to the back of the queue for a retry
            session.commit()
            self.report_progress()

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
                      landscape: bool, note: str = "") -> str:
        spec = plan.get("planner_spec") or {}
        user = (f"Design brief:\n{json.dumps(spec, indent=2)}\n\nPalette (background, ink, accent): {palette}\n"
                f"Audience: {brief.get('audience')}\nStyle: {brief.get('style')}\n"
                f"Orientation: {'landscape' if landscape else 'portrait'}\nProduct title: {plan.get('title_hint')}{note}")
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
    def _card(title: str, items: list[str], palette: list[str], **kw) -> bytes:
        """'What's included' card rendered to fit its content, then framed at listing size."""
        png = render.html_to_png(mockups.included_html(title, items, palette=palette, **kw), width=1000, height=1000,
                                 full_page=True)
        return mockups.primary_image(mockups.load(png), bg=palette[0])

    def _snapshots(self, spec: dict, palette: list[str]) -> dict[str, bytes]:
        try:
            return sheet_snapshots(spec, palette=palette)
        except Exception as e:  # noqa: BLE001 - previews fall back to the HTML renderer
            self.log.warning("real-file snapshots failed; using HTML previews", error=str(e)[:200])
            return {}

    def _build_workbook(self, session, product: Product, spec: dict, palette: list[str],
                        context: str) -> tuple[dict, bytes, dict]:
        """Build, then prove the workbook is clean (every formula valid, zero errors after a real
        recalculation). Problems go back to Claude to fix; a file that never passes is not shipped."""
        if not office.available() and self.settings.app_env != "local":
            raise RuntimeError("LibreOffice is not installed, so the spreadsheet cannot be verified; refusing to ship it")
        problems: list[str] = []
        for attempt in range(MAX_SHEET_FIXES + 1):
            problems = spec_problems(spec, palette)
            check: dict = {}
            if not problems:
                xlsx = render_spreadsheet(spec, palette=palette, strict=True)
                check = validate_xlsx(xlsx)
                if check["ok"]:
                    return spec, xlsx, check
                problems = check["errors"]
            self.log.info("spreadsheet problems found; asking for a fix", product_id=product.id, attempt=attempt + 1,
                          problems=problems[:5])
            if attempt == MAX_SHEET_FIXES:
                break
            fix = self.ask(session, task="creator_sheet_fix", model=self.settings.model_worker, max_tokens=16000,
                           user=(f"{context}\n\nYour spreadsheet_spec failed validation (cells are reported after a "
                                 "full recalculation, data rows start at row 4):\n- " + "\n- ".join(problems[:20]) +
                                 "\n\nReturn ONLY the corrected spreadsheet_spec JSON object (same schema). "
                                 f"Keep everything that works.\n\n{json.dumps(spec)}"))
            product.add_cost(fix.cost_usd)
            try:
                fixed = fix.json()
            except LLMError:
                continue
            if isinstance(fixed, dict):
                spec = fixed.get("spreadsheet_spec") if isinstance(fixed.get("spreadsheet_spec"), dict) else fixed
        raise SpreadsheetError([f"spreadsheet failed validation after {MAX_SHEET_FIXES} fixes"] + problems[:5])

    # ------------------------------------------------------------------ in-house apparel / mugs
    def _merch_art(self, session, product: Product, spec: dict, images) -> bytes | None:
        prompt = str(spec.get("illustration_prompt") or "").strip()
        if not prompt:
            return None
        if not images.is_real and not self._is_mock_run:
            raise RuntimeError("this design needs an illustration but no real IMAGE_PROVIDER is configured")
        gen = images.generate(prompt + "\nIsolated artwork for printing on merchandise: transparent background, "
                              "no mockup, no product, no text or lettering, crisp clean edges.",
                              size=(1024, 1024), transparent=True,
                              negative_prompt="text, letters, watermark, logo, brand, background scene, t-shirt, mug")
        product.add_cost(gen.cost_usd)
        self.record_cost(session, gen.cost_usd, kind="image")
        return gen.data

    def _design_merch(self, session, product: Product, spec: dict, item, art_uri: str | None, note: str) -> tuple[str, render.PrintRender]:
        """Claude designs the print as HTML/CSS; we render it at print resolution and send problems back."""
        w, h = item.print_area["width_in"], item.print_area["height_in"]
        fields = (spec.get("personalization") or {}).get("fields") or []
        example = {str(f.get("key")): str(f.get("example") or f.get("label") or "Name") for f in fields if f.get("key")}
        stress = {k: ("Mw" * 20)[:int(next((f.get("max_chars") for f in fields if f.get("key") == k), 12) or 12)] for k in example}
        colors = {c: item.colors[c] for c in spec.get("colors") or [] if c in item.colors}
        user = (f"Design brief:\n{json.dumps(spec, indent=2)}\n\nProduct: {item.label} ({item.method}); print area "
                f"{item.print_area['name']} {w} x {h} in.\nBlank colours it will be printed on: {json.dumps(colors)}\n"
                f"Illustration available as {{{{art}}}}: {'yes' if art_uri else 'no'}\n"
                f"Personalisation placeholders: {', '.join('{{' + k + '}}' for k in example) or 'none'}"
                + ("\nMug: the wrap is seen from the front; keep the main design within the middle 40% of the width "
                   "(you may repeat a smaller motif on the far left/right)." if item.product_type == "mug" else "")
                + note)
        result = self.ask(session, task="creator_merch_html", model=self.settings.model_worker, user=user,
                          max_tokens=12000, prompt="creator_merch")
        product.add_cost(result.cost_usd)
        html = render.extract_html(result.text)
        problems: list[str] = []
        for attempt in range(MAX_LAYOUT_FIXES + 1):
            sample = render.render_print_file(html, width_in=w, height_in=h, dpi=get_catalog().min_dpi,
                                              values=example, art_uri=art_uri)
            problems = list(sample.problems)
            if example:
                long = render.render_print_file(html, width_in=w, height_in=h, dpi=40, values=stress, art_uri=art_uri)
                problems += [f"with a {len(next(iter(stress.values())))}-character name: {p}" for p in long.problems
                             if "only uses" not in p]  # fit/edge problems only; coverage is judged on the sample
            problems += self._contrast_problems(item, sample.png, list(colors))
            if not problems:
                return html, sample
            self.log.info("merch design problems; asking for a fix", product_id=product.id, problems=problems[:5])
            if attempt == MAX_LAYOUT_FIXES:
                break
            fix = self.ask(session, task="creator_merch_fix", model=self.settings.model_worker, max_tokens=12000,
                           prompt="creator_merch",
                           user=(f"{user}\n\nYour previous design rendered with these problems:\n- " + "\n- ".join(problems[:10])
                                 + f"\n\nFix every problem. Return the full corrected document.\n\n```html\n{html}\n```"))
            product.add_cost(fix.cost_usd)
            html = render.extract_html(fix.text)
        raise RuntimeError(f"print design still has problems after {MAX_LAYOUT_FIXES} fixes: {problems[:3]}")

    @staticmethod
    def _contrast_problems(item, png: bytes, colors: list[str]) -> list[str]:
        weak = [c for c in colors if merch.contrast(item, png, c) < 0.22]
        if colors and len(weak) == len(colors):
            return [f"the ink colours barely show on every chosen blank ({', '.join(weak)}); change the ink colours "
                    "or the blank colours"]
        return []

    def _photo(self, session, product: Product, images, flat: Image.Image, item, color: str) -> Image.Image | None:
        if not (images.is_real and self.settings.mockup_scenes):
            return None
        noun = "mug" if item.product_type == "mug" else f"{color.lower()} {item.label.lower()}"
        prompt = (f"Professional Etsy product photo of this exact {noun}. Keep the printed design EXACTLY as it is: "
                  "same words, spelling, colours, layout and position; do not add, remove or redraw anything in the print. "
                  + ("Place the mug on a styled table with soft morning light, a few tasteful props, shallow depth of field."
                     if item.product_type == "mug" else
                     "Show it as a neat flat lay on a light wooden surface with soft natural light and a couple of "
                     "tasteful props, realistic fabric texture and folds.")
                  + " No people, no other text, no logos, no watermark.")
        try:
            gen = images.edit(prompt, merch.to_png(flat),
                              size=(1024, 1024), quality=self.settings.mockup_quality)
        except Exception as e:  # noqa: BLE001 - a photo is a bonus; the exact mockup is always there
            self.log.warning("photo mockup failed; using the flat mockup", error=str(e)[:200])
            return None
        product.add_cost(gen.cost_usd)
        self.record_cost(session, gen.cost_usd, kind="image")
        return mockups.load(gen.data)

    def _create_merch(self, session, product: Product, brief: dict, plan: dict, palette: list[str], title: str,
                      base: str, slug: str, storage, images, files: list[str], imgs: list[str], note: str,
                      scene_key: int) -> dict:
        import base64

        catalog = get_catalog()
        spec = dict(plan.get("merch_spec") or {})
        item = catalog.get(spec.get("item") or brief.get("item"))
        if item is None or item.product_type != product.product_type:
            options = [i.key for i in catalog.for_type(product.product_type)]
            if not options:
                raise RuntimeError(f"catalog.yaml has no items for {product.product_type}")
            item = catalog.get(options[0])
        spec["item"] = item.key
        spec["colors"] = [c for c in spec.get("colors") or [] if c in item.colors][:8] or list(item.colors)[:4]
        pers = spec.get("personalization") or {}
        fields = [f for f in pers.get("fields") or [] if isinstance(f, dict) and re.fullmatch(r"[a-z_][a-z0-9_]*", str(f.get("key", "")))]
        spec["personalization"] = {**pers, "enabled": bool(pers.get("enabled") and fields), "fields": fields[:3]}

        art = self._merch_art(session, product, spec, images)
        art_path = storage.put(f"{base}/source/art.png", art, "image/png") if art else None
        art_uri = f"data:image/png;base64,{base64.b64encode(art).decode()}" if art else None
        html, sample = self._design_merch(session, product, spec, item, art_uri, note)
        hits = get_blocklist().check_many(re.sub(r"<[^>]+>", " ", html))
        if hits:
            raise RuntimeError(f"blocklist hit in design text: {hits}")
        # keep only blank colours the design actually reads on
        spec["colors"] = [c for c in spec["colors"] if merch.contrast(item, sample.png, c) >= 0.22] or spec["colors"]
        template = storage.put(f"{base}/source/design.html", html.encode("utf-8"), "text/html")
        personalised = spec["personalization"]["enabled"]
        name = f"print-{slug}-{'example' if personalised else 'final'}.png"
        files.append(storage.put(f"{base}/{name}", sample.png, "image/png"))

        hero = merch.best_contrast_color(item, sample.png, spec["colors"])
        flat = merch.mockup(item, sample.png, hero)
        photo = self._photo(session, product, images, flat, item, hero)
        imgs.append(storage.put(f"{base}/listing-01-primary.png",
                                mockups.primary_image(photo or flat, bg="#F3F0EB"), "image/png"))
        if photo:
            imgs.append(storage.put(f"{base}/listing-02-mockup.png", merch.to_png(flat), "image/png"))
        if len(spec["colors"]) > 1:
            imgs.append(storage.put(f"{base}/listing-03-colours.png",
                                    merch.to_png(merch.color_chart(item, sample.png, spec["colors"])), "image/png"))
        imgs.append(storage.put(f"{base}/listing-04-closeup.png",
                                merch.to_png(merch.design_closeup(sample.png, item.colors[hero])), "image/png"))
        days = catalog.processing_days
        if personalised:
            how = [f"{f.get('label') or f['key']}: up to {f.get('max_chars', 20)} characters (e.g. {f.get('example', '')})"
                   for f in spec["personalization"]["fields"]]
            how += ["Type it in the personalisation box at checkout", "We double-check spelling before printing"]
            imgs.append(storage.put(f"{base}/listing-05-personalise.png", self._card(
                title, how, palette, kicker="How to personalise", foot="Every order is printed just for you"), "image/png"))
        details = [item.label, item.material, f"{item.method} print, made to order",
                   f"Sizes: {', '.join(item.sizes)}" if len(item.sizes) > 1 else f"Size: {item.sizes[0]}",
                   f"Ships in {days[0]}-{days[-1]} business days", "Printed by our family print shop"]
        imgs.append(storage.put(f"{base}/listing-06-details.png", self._card(
            title, details, palette, kicker="Product details", foot="Made to order · ships from Arizona"), "image/png"))
        return {**plan, "merch_spec": spec, "merch": {
            "item": item.key, "template": template, "art": art_path, "print_px": [sample.width_px, sample.height_px],
            "dpi": catalog.min_dpi, "hero_color": hero, "example_values": {
                str(f["key"]): str(f.get("example") or "") for f in spec["personalization"]["fields"]}}}

    # ------------------------------------------------------------------ main
    def _create_one(self, session, product: Product, storage, blocklist, images) -> None:
        full = dict(product.brief_json or {})
        brief = {k: v for k, v in full.items() if k not in INTERNAL_KEYS}
        mock_hint = " (This is a MOCK run; placeholder art is expected.)" if self._is_mock_run else ""
        comments = session.execute(select(OwnerNote).where(
            OwnerNote.product_id == product.id, OwnerNote.kind == OwnerNoteKind.COMMENT,
            OwnerNote.status.is_(None))).scalars().all()
        owner_notes = ("\n\nOWNER NOTES on this product (apply them):\n" + "\n".join(f"- {c.text}" for c in comments)) \
            if comments and not full.get("revision") else ""
        user = (f"Brief:\n{json.dumps({**brief, 'product_type': product.product_type}, indent=2)}{mock_hint}"
                f"{revision_note(full)}{owner_notes}")
        if product.product_type in ProductType.PHYSICAL:
            items = [i.summary() for i in get_catalog().for_type(product.product_type)]
            user += f"\n\nCatalog items you may use for this product (`merch_spec.item`):\n{json.dumps(items, indent=1)}"
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
                                    json.dumps(plan.get("spreadsheet_spec") or {}), json.dumps(plan.get("merch_spec") or {}))
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
            html = self._design_pages(session, product, brief, plan, palette, landscape, revision_note(full) + owner_notes)
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
            spec, xlsx, check = self._build_workbook(session, product, spec, palette, user)
            plan = {**plan, "spreadsheet_spec": spec, "file_check": check}
            files.append(storage.put(f"{base}/{slug}.xlsx", xlsx, XLSX_MIME))
            sheets = spec.get("sheets") or []
            tabs = check.get("sheets") or [str(s.get("name")) for s in sheets]
            dash_name = (spec.get("dashboard") or {}).get("name") or "Dashboard"
            snaps = self._snapshots(spec, palette)
            hero_tab = next((t for t in tabs if t in snaps and t.lower() == str(dash_name).lower()), None) or \
                next((t for t in tabs[1:] if t in snaps), None)

            def framed(tab: str, subtitle: str) -> Image.Image:
                return mockups.load(render.html_to_png(window_html(
                    snaps[tab], title=title, subtitle=subtitle, tabs=tabs, active=tab, palette=palette),
                    width=1100, height=700, full_page=True))

            def shot(idx: list[int]) -> Image.Image:
                return mockups.load(render.html_to_png(preview_html(spec, palette=palette, sheets=idx),
                                                       width=1100, height=700, full_page=True))

            hero = framed(hero_tab, "Dashboard updates automatically · Excel & Google Sheets") if hero_tab else shot([0, 1])
            scene = self._scene(session, storage, images, "desk", scene_key, product)
            imgs.append(storage.put(f"{base}/listing-01-primary.png", mockups.primary_image(hero, bg=palette[0]), "image/png"))
            imgs.append(storage.put(f"{base}/listing-02-lifestyle.png",
                                    mockups.scene_composite(hero, scene, framed=True, rotate=0) if scene
                                    else mockups.styled_flat_lay(hero, accent=palette[2]), "image/png"))
            data_tabs = [t for t in tabs if t in snaps and t not in ("Start Here", "Settings", hero_tab)]
            if data_tabs:  # real renders of the data tabs (true formula results, real styling)
                for n, tab in enumerate(data_tabs[:2]):
                    name = "listing-03-tabs.png" if n == 0 else "listing-04-more-tabs.png"
                    imgs.append(storage.put(f"{base}/{name}", mockups.primary_image(
                        framed(tab, "Example entries included · type over them with your own"), bg=palette[0]), "image/png"))
            else:
                imgs.append(storage.put(f"{base}/listing-03-tabs.png",
                                        mockups.primary_image(shot([0, 1]), bg=palette[0]), "image/png"))
                if len(sheets) > 2:  # show every remaining tab so buyers (and QA) see all features
                    imgs.append(storage.put(f"{base}/listing-04-more-tabs.png",
                                            mockups.primary_image(shot(list(range(2, min(len(sheets), 4)))), bg=palette[0]),
                                            "image/png"))
            if "Start Here" in snaps:
                imgs.append(storage.put(f"{base}/listing-05-instructions.png", mockups.primary_image(
                    framed("Start Here", "Step-by-step instructions built in"), bg=palette[0]), "image/png"))
            sheet_names = [t for t in tabs if t != "Settings"][:6]
            included = [f"{len(tabs)} tabs: " + ", ".join(sheet_names)]
            if spec.get("dashboard"):
                included.append("Automatic dashboard with charts")
            included += ["Dropdowns you can customise", "Formulas and totals built in",
                         "Opens in Excel and Google Sheets", "Example entries to show how it works"]
            imgs.append(storage.put(f"{base}/listing-06-included.png", self._card(title, included, palette), "image/png"))
        elif product.product_type in ProductType.PHYSICAL:
            plan = self._create_merch(session, product, brief, plan, palette, title, base, slug, storage, images,
                                      files, imgs, revision_note(full) + owner_notes, scene_key)
        else:
            raise RuntimeError(f"product_type {product.product_type} is Phase 2 (POD) and not implemented in v1")

        product.file_paths = files
        product.image_paths = imgs
        done = list(full.get("revisions_done") or [])
        if full.get("revision"):
            rev = {k: v for k, v in full["revision"].items() if k != "previous_plan"}
            done.append({**rev, "completed_run": self.run_id})
        product.brief_json = {**brief, "creator_plan": plan, **({"revisions_done": done} if done else {})}
        for c in comments:
            c.status = "included"
        for n in session.execute(select(OwnerNote).where(OwnerNote.product_id == product.id,
                                                         OwnerNote.kind == OwnerNoteKind.REVISION,
                                                         OwnerNote.status == "open")).scalars():
            n.status = "addressed"
        self.log.info("product created", product_id=product.id, files=len(files), images=len(imgs))
