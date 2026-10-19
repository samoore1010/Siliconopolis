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
        product.phash = compute_phash(primary)
        others = session.execute(
            select(Product.id, Product.phash).where(Product.id != product.id, Product.phash.is_not(None),
                                                    Product.state.in_([ProductState.APPROVED, ProductState.DRAFTED, ProductState.PUBLISHED]))
        ).all()
        for other_id, other_hash in others:
            if other_hash and hamming(product.phash, other_hash) <= PHASH_DISTANCE_THRESHOLD:
                return {"pass": False, "reasons": [f"near-duplicate of product {other_id} (phash distance <= {PHASH_DISTANCE_THRESHOLD})"],
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
        rev = brief.get("revision") or (brief.get("revisions_done") or [{}])[-1]
        owner = f"\n\nThe shop owner asked for these changes in this version; check they were made:\n{rev.get('feedback')}" \
            if rev.get("feedback") else ""
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
        passed = bool(verdict.get("pass")) and not verdict.get("ip_concerns") and not lows and not missing
        reasons.extend(verdict.get("reasons", []))
        if verdict.get("ip_concerns"):
            reasons.append(f"ip concerns: {verdict['ip_concerns']}")
        if lows:
            reasons.append("below the bar: " + ", ".join(
                f"{k} {scores[k]} (needs {MIN_COMMERCIAL_SCORE if k in COMMERCIAL_KEYS else MIN_SCORE})" for k in lows))
        if missing:
            reasons.append(f"QA did not score {', '.join(missing)}; rejecting on doubt")
        return {"pass": passed, "scores": scores, "reasons": reasons, "ip_concerns": verdict.get("ip_concerns", []),
                "verdict": verdict.get("verdict"), "improvements": verdict.get("improvements", []),
                "reviewed": {"listing_images": len(listing), "file_images": len(files), "file": file_note},
                "file_check": file_check,
                "checks": {"blocklist": "ok", "duplicate": "ok", "file": "ok", "vision": "ok" if passed else "fail"}}

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
            if first.endswith(".png"):
                return {"ok": True}, [storage.get(first)], "the delivered print file"
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "errors": [f"could not open delivered file: {str(e)[:200]}"]}, [], "the file"
        return None, [], "not rendered"
