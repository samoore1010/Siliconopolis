"""QA: created -> approved | rejected. Blocklist + perceptual-hash duplicate check + Claude vision."""

from __future__ import annotations

import io
import json

import imagehash
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.llm import ImageInput
from pipeline.storage import get_storage

from .base import Agent

PHASH_DISTANCE_THRESHOLD = 6  # <= 6 bits differ => near duplicate
MIN_SCORE = 6


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

        # 3. vision review with Claude
        images = [ImageInput(data=storage.get(p)) for p in product.image_paths[:3]]
        mock_hint = "MOCK RUN: placeholder images are expected and acceptable." if self.settings.mode == "mock" else "LIVE RUN."
        user = (f"{mock_hint}\n\nBrief:\n{json.dumps({k: v for k, v in brief.items() if k != 'creator_plan'}, indent=2)}\n\n"
                f"Product type: {product.product_type}\nReview the attached listing images.")
        result = self.ask(session, task="qa_vision", model=self.settings.model_worker, user=user, images=images, temperature=0.0)
        product.add_cost(result.cost_usd)
        verdict = result.json()
        scores = verdict.get("scores", {}) or {}
        passed = bool(verdict.get("pass")) and not verdict.get("ip_concerns") and all(
            float(v) >= MIN_SCORE for v in scores.values() if isinstance(v, (int, float)))
        reasons.extend(verdict.get("reasons", []))
        if verdict.get("ip_concerns"):
            reasons.append(f"ip concerns: {verdict['ip_concerns']}")
        if not passed and bool(verdict.get("pass")):
            reasons.append(f"a score fell below {MIN_SCORE}; rejecting on doubt")
        return {"pass": passed, "scores": scores, "reasons": reasons, "ip_concerns": verdict.get("ip_concerns", []),
                "checks": {"blocklist": "ok", "duplicate": "ok", "vision": "ok" if passed else "fail"}}
