"""QA: created -> approved | rejected. Blocklist + perceptual-hash duplicate check + Claude vision."""

from __future__ import annotations

import io
import json

import imagehash
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState, ProductType
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.llm import ImageInput
from pipeline.storage import get_storage

from .base import Agent

PHASH_DISTANCE_THRESHOLD = 6  # <= 6 bits differ => near duplicate
MIN_SCORE = 6
# The sellability bar: would a buyer choose this over the best sellers at the target price?
COMMERCIAL_KEYS = ("design_polish", "commercial_viability")
MIN_COMMERCIAL_SCORE = 8
MAX_FILE_IMAGES = 6


def _shrink(png: bytes, long_side: int = 1400) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((long_side, long_side))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def compute_phash(png: bytes) -> str:
    return str(imagehash.phash(Image.open(io.BytesIO(png)).convert("RGB")))


def hamming(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


class QAAgent(Agent):
    name = "qa"
    prompt_file = "qa"

    def __init__(self, *a, batch_size: int = 20, **kw):
        super().__init__(*a, **kw)
        self.batch_size = batch_size

    def execute(self, session: Session) -> None:
        owner = f"qa:{self.run_id}"
        products = claim_products(session, state=ProductState.CREATED, owner=owner, limit=self.batch_size,
                                  lock_timeout_minutes=self.settings.lock_timeout_minutes)
        self.log.info("claimed created products", count=len(products))
        storage = get_storage()
        blocklist = get_blocklist()
        for product in products:
            try:
                verdict = self._review(session, product, storage, blocklist)
                product.qa_result_json = verdict
                new_state = ProductState.APPROVED if verdict["pass"] else ProductState.REJECTED
                transition(session, product, new_state)
                self.items_processed += 1
                self.log.info("qa verdict", product_id=product.id, state=new_state, reasons=verdict.get("reasons"))
            except CapReached:
                release(session, product)
                session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                self.note_error(f"qa failed for product {product.id}: {e}", product_id=product.id)
                release(session, product)
            session.commit()

    def _review(self, session, product: Product, storage, blocklist) -> dict:
        reasons: list[str] = []
        brief = product.brief_json
        plan = brief.get("creator_plan", {})

        # 1. blocklist on everything textual we generated
        hits = blocklist.check_many(json.dumps(brief), json.dumps(plan))
        if hits:
            return {"pass": False, "reasons": [f"blocklist hit: {hits}"], "scores": {}, "checks": {"blocklist": "fail"}}

        # 2. near-duplicate check against catalog
        if not product.image_paths:
            return {"pass": False, "reasons": ["no listing images"], "scores": {}, "checks": {"images": "missing"}}
        primary = storage.get(product.image_paths[0])
        if product.product_type in ProductType.PHYSICAL and product.file_paths:
            # mockups share the same blank silhouette; compare the designs themselves
            art = Image.open(io.BytesIO(storage.get(product.file_paths[0]))).convert("RGBA")
            flat = Image.new("RGBA", art.size, (255, 255, 255, 255))
            flat.alpha_composite(art)
            buf = io.BytesIO()
            flat.convert("RGB").save(buf, "PNG")
            primary = buf.getvalue()
        product.phash = compute_phash(primary)
        others = session.execute(
            select(Product.id, Product.phash).where(Product.id != product.id, Product.phash.is_not(None),
                                                    Product.state.in_([ProductState.APPROVED, ProductState.DRAFTED, ProductState.PUBLISHED]))
        ).all()
        for other_id, other_hash in others:
            if other_hash and hamming(product.phash, other_hash) <= PHASH_DISTANCE_THRESHOLD:
                return {"pass": False, "reasons": [f"Too similar to product #{other_id}, which is already approved or on Etsy (near-duplicate image)"],
                        "scores": {}, "checks": {"duplicate": "fail"}}

        # 3. the delivered file itself: must open cleanly, and QA looks at what the buyer actually gets
        file_check, file_pngs, file_note = self._inspect_files(product, storage)
        if file_check is not None and not file_check.get("ok", True):
            return {"pass": False, "reasons": ["delivered file is broken: " + "; ".join(file_check.get("errors", [])[:5])],
                    "scores": {}, "checks": {"blocklist": "ok", "duplicate": "ok", "file": "fail"}, "file_check": file_check}

        # 4. vision review with Claude: listing images + pages/tabs of the real file
        listing = [ImageInput(data=_shrink(storage.get(p))) for p in product.image_paths[:3]]
        files = [ImageInput(data=_shrink(p)) for p in file_pngs[:MAX_FILE_IMAGES]]
        is_mock = self.settings.mode == "mock" and self.settings.app_env == "local"
        mock_hint = "MOCK RUN: placeholder images are expected and acceptable." if is_mock else "LIVE RUN: these are the real product images that will be listed."
        market = brief.get("market") or {}
        owner, asked = self._change_request(brief)
        brief_view = {k: v for k, v in brief.items() if k not in ("creator_plan", "revision", "revisions_done", "market")}
        user = (f"{mock_hint}\n\nBrief:\n{json.dumps(brief_view, indent=2)}\n\n"
                f"Market context: price range {market.get('price_range') or 'unknown'}; "
                f"{market.get('summary') or ''}\nTarget price: ${brief.get('target_price_usd', '?')}\n\n"
                f"Product type: {product.product_type}\n"
                f"Images 1-{len(listing)} are the listing images. Images {len(listing) + 1}-{len(listing) + len(files)} "
                f"are {file_note}.{owner}")
        result = self.ask(session, task="qa_vision", model=self.settings.model_worker, user=user,
                          images=listing + files, temperature=0.0)
        product.add_cost(result.cost_usd)
        verdict = result.json()
        scores = verdict.get("scores", {}) or {}

        def low(key: str, value) -> bool:
            if not isinstance(value, (int, float)):
                return False
            return float(value) < (MIN_COMMERCIAL_SCORE if key in COMMERCIAL_KEYS else MIN_SCORE)

        missing = [k for k in COMMERCIAL_KEYS if k not in scores]
        lows = [k for k, v in scores.items() if low(k, v)]
        checked = [c for c in verdict.get("requested_changes") or [] if isinstance(c, dict)]
        not_done = [str(c.get("change")) for c in checked if not c.get("done")]
        unchecked = asked and not checked
        passed = (bool(verdict.get("pass")) and not verdict.get("ip_concerns") and not lows and not missing
                  and not not_done and not unchecked)
        reasons.extend(verdict.get("reasons", []))
        if not_done:
            reasons.append("requested changes not made: " + "; ".join(not_done))
        if unchecked:
            reasons.append("QA did not confirm the requested changes were made; rejecting on doubt")
        if verdict.get("ip_concerns"):
            reasons.append(f"ip concerns: {verdict['ip_concerns']}")
        if lows:
            reasons.append("below the bar: " + ", ".join(
                f"{k} {scores[k]} (needs {MIN_COMMERCIAL_SCORE if k in COMMERCIAL_KEYS else MIN_SCORE})" for k in lows))
        if missing:
            reasons.append(f"QA did not score {', '.join(missing)}; rejecting on doubt")
        return {"pass": passed, "scores": scores, "reasons": reasons, "ip_concerns": verdict.get("ip_concerns", []),
                "verdict": verdict.get("verdict"), "improvements": verdict.get("improvements", []),
                "requested_changes": checked,
                "reviewed": {"listing_images": len(listing), "file_images": len(files), "file": file_note},
                "file_check": file_check,
                "checks": {"blocklist": "ok", "duplicate": "ok", "file": "ok", "vision": "ok" if passed else "fail"}}

    @staticmethod
    def _change_request(brief: dict) -> tuple[str, list[str]]:
        """The most recent owner revision or Editor polish, as text for the prompt and as a checklist.
        QA must confirm each item was actually made in the files it is looking at."""
        from pipeline.agents.editor import requested_changes

        pending_rev = brief.get("revision")
        last_rev = (brief.get("revisions_done") or [{}])[-1]
        last_edit = (brief.get("edits_done") or [{}])[-1]
        if pending_rev:
            rev, edit = pending_rev, {}
        elif int(last_edit.get("completed_run") or 0) > int(last_rev.get("completed_run") or 0):
            rev, edit = {}, last_edit
        else:
            rev, edit = last_rev, {}
        if rev.get("feedback"):
            items = [ln.lstrip("- ").strip() for ln in str(rev["feedback"]).splitlines() if ln.strip()]
            text = ("\n\nThe shop owner asked the Creator to rebuild this with these changes. REQUESTED CHANGES "
                    "(confirm each one in the files):\n" + "\n".join(f"- {i}" for i in items))
            return text, items
        if edit and not edit.get("failed"):
            items = requested_changes(edit)
            prev = edit.get("previous_qa") or {}
            text = ("\n\nThis is a POLISHED version by the Editor. REQUESTED CHANGES (confirm each one in the files; "
                    "an ignored request fails the product):\n" + ("\n".join(f"- {i}" for i in items) or "- (none; general polish)")
                    + (f"\nPrevious version's scores: {json.dumps(prev.get('scores'))}. The polish should score at least as high."
                       if prev.get("scores") else ""))
            return text, items
        return "", []

    def _inspect_files(self, product: Product, storage) -> tuple[dict | None, list[bytes], str]:
        """Validate the delivered file and render it to images (what the buyer will open)."""
        from pipeline.generators import office
        from pipeline.generators.xlsx import sheet_snapshots, validate_xlsx

        if not product.file_paths:
            return {"ok": False, "errors": ["no deliverable files"]}, [], "missing"
        first = product.file_paths[0]
        plan = (product.brief_json or {}).get("creator_plan") or {}
        try:
            if product.product_type == ProductType.SPREADSHEET:
                check = validate_xlsx(storage.get(first))
                if not check["ok"]:
                    return check, [], "the spreadsheet"
                snaps = sheet_snapshots(plan.get("spreadsheet_spec") or {}, palette=plan.get("palette"))
                return check, list(snaps.values()), (
                    "every tab of the delivered workbook, rendered by a spreadsheet app (Start Here, Dashboard, "
                    "data tabs, Settings)" if snaps else "unavailable (no renderer)")
            if first.endswith(".pdf"):
                pages = office.pdf_to_pngs(storage.get(first), scale=1.2, max_pages=MAX_FILE_IMAGES)
                if not pages:
                    return {"ok": False, "errors": ["PDF has no pages"]}, [], "the PDF"
                return {"ok": True, "pages": len(pages)}, pages, "the pages of the delivered PDF (US Letter)"
            if product.product_type in ProductType.PHYSICAL:
                from pipeline.catalog import get_catalog
                from pipeline.generators import merch

                data = storage.get(first)
                img = Image.open(io.BytesIO(data))
                item = get_catalog().get((plan.get("merch") or {}).get("item"))
                errors = []
                if item:
                    need_w, need_h = item.print_px
                    if img.width < need_w * 0.98 or img.height < need_h * 0.98:
                        errors.append(f"print file is {img.width}x{img.height}px; {item.label} needs "
                                      f"{need_w}x{need_h}px ({get_catalog().min_dpi} dpi)")
                if img.mode != "RGBA" or img.getchannel("A").getextrema()[0] > 250:
                    errors.append("print file has no transparent background")
                check = {"ok": not errors, "errors": errors, "size_px": [img.width, img.height]}
                hero = (plan.get("merch") or {}).get("hero_color")
                views = [data] + ([merch.to_png(merch.design_closeup(data, item.colors[hero]))]
                                  if item and hero in item.colors else [])
                return check, views, ("the print file exactly as it will be printed (transparent background), then "
                                      "the same file on the blank colour")
            if first.endswith(".png"):
                return {"ok": True}, [storage.get(first)], "the delivered print file"
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "errors": [f"could not open delivered file: {str(e)[:200]}"]}, [], "the file"
        return None, [], "not rendered"
