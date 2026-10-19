"""Lister: approved -> Etsy DRAFT listing (state=drafted). Never publishes."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.db.events import EventKind
from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import Product, ProductState, ProductType
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.guardrails.caps import remaining_daily_drafts
from pipeline.storage import get_storage

from .base import Agent

AI_DISCLOSURE = (
    "\n\n— Transparency note —\nThis design was created by the shop owner with the help of AI tools "
    "(for ideation, imagery and/or copy), then reviewed before listing. It is an original digital "
    "product and does not reference any brand, character or artist."
)

# TODO(owner): confirm these against Etsy's current seller policy for AI-assisted, designed-by-seller
# digital items before go-live (who_made / when_made / is_supply / production partner disclosure).
PRODUCTION_ATTRIBUTES = {"who_made": "i_did", "when_made": "2020_2026", "is_supply": False}

TAXONOMY_HINTS = {
    ProductType.WALL_ART: ["digital prints", "prints", "wall decor"],
    ProductType.PLANNER: ["calendars & planners", "planners", "paper"],
    ProductType.TRACKER: ["calendars & planners", "planners", "paper"],
    ProductType.SPREADSHEET: ["spreadsheets", "digital templates", "templates"],
}


class ListerAgent(Agent):
    name = "lister"
    prompt_file = "lister"

    def execute(self, session: Session) -> None:
        remaining = remaining_daily_drafts(session, self.settings.daily_draft_cap)
        if remaining <= 0:
            raise CapReached("DAILY_DRAFT_CAP", self.settings.daily_draft_cap, self.settings.daily_draft_cap)
        etsy = get_etsy_client()
        if not etsy.is_connected():
            waiting = session.execute(
                select(func.count(Product.id)).where(Product.state == ProductState.APPROVED)
            ).scalar_one()
            msg = (f"Etsy not connected yet (app approval / OAuth pending): {waiting} approved products "
                   "waiting; they will be drafted automatically once Etsy is connected.")
            self.log.warning(msg, waiting=waiting)
            self._event(EventKind.INFO, msg)
            return
        owner = f"lister:{self.run_id}"
        products = claim_products(session, state=ProductState.APPROVED, owner=owner, limit=remaining,
                                  lock_timeout_minutes=self.settings.lock_timeout_minutes,
                                  product_types=ProductType.DIGITAL)
        self.log.info("claimed approved products", count=len(products), daily_remaining=remaining)
        storage = get_storage()
        blocklist = get_blocklist()
        taxonomy = {n.id: n for n in etsy.flatten_taxonomy()}

        for product in products:
            try:
                self._list_one(session, product, etsy, storage, blocklist, taxonomy)
                transition(session, product, ProductState.DRAFTED)
                self.items_processed += 1
            except CapReached:
                release(session, product)
                session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                self.note_error(f"lister failed for product {product.id}: {e}", product_id=product.id)
                release(session, product)
            session.commit()

    def _pick_taxonomy(self, product_type: str, hint: str, taxonomy: dict) -> int | None:
        wanted = [hint.lower()] + TAXONOMY_HINTS.get(product_type, [])
        for w in wanted:
            for node in taxonomy.values():
                if w and w in node.full_path.lower():
                    return node.id
        return next(iter(taxonomy), None)

    def _list_one(self, session, product: Product, etsy, storage, blocklist, taxonomy) -> None:
        brief = {k: v for k, v in product.brief_json.items() if k != "creator_plan"}
        plan = product.brief_json.get("creator_plan", {})
        user = (f"Brief:\n{json.dumps(brief, indent=2)}\n\nProduction plan summary:\n"
                f"{json.dumps({k: plan.get(k) for k in ('title_hint', 'planner_spec', 'spreadsheet_spec')}, indent=2)}\n\n"
                f"Files delivered: {[PurePosixPath(p).name for p in product.file_paths]}")
        result = self.ask(session, task="lister_copy", model=self.settings.model_worker, user=user)
        product.add_cost(result.cost_usd)
        copy = result.json()

        title = str(copy.get("title", "")).strip()[:140]
        tags = [str(t).strip().lower()[:20] for t in copy.get("tags", []) if str(t).strip()]
        tags = list(dict.fromkeys(tags))[:13]
        if len(tags) < 13:
            fallback = [k.lower()[:20] for k in brief.get("keywords", [])] + ["digital download", "printable", "instant download"]
            for f in fallback:
                if len(tags) >= 13:
                    break
                if f not in tags:
                    tags.append(f)
        description = str(copy.get("description", "")).strip() + AI_DISCLOSURE
        if not title:
            raise RuntimeError("lister: empty title")
        hits = blocklist.check_many(title, " ".join(tags), description)
        if hits:
            raise RuntimeError(f"lister: blocklist hit in copy: {hits}")

        price = float(brief.get("target_price_usd") or 5.99)
        taxonomy_id = self._pick_taxonomy(product.product_type, str(copy.get("taxonomy_hint", "")), taxonomy)
        payload = {
            "title": title, "description": description, "tags": tags, "price": round(price, 2), "quantity": 999,
            "taxonomy_id": taxonomy_id, "type": "download", "materials": copy.get("materials", ["digital file"])[:13],
            "state": "draft", **PRODUCTION_ATTRIBUTES,
        }
        if self.settings.dry_run:
            self.log.info("DRY_RUN: would create draft listing", product_id=product.id, title=title)
            product.listing_json = {**payload, "dry_run": True}
            return

        draft = etsy.create_draft_listing(payload)
        for rank, img in enumerate(product.image_paths[:10], start=1):
            etsy.upload_listing_image(draft.listing_id, storage.get(img), PurePosixPath(img).name, rank=rank)
        for f in product.file_paths[:5]:  # Etsy allows up to 5 digital files per listing
            name = PurePosixPath(f).name
            etsy.upload_listing_file(draft.listing_id, storage.get(f), name, name=name[:70])

        product.etsy_listing_id = draft.listing_id
        product.listing_json = {**payload, "etsy_url": draft.url, "listing_state": draft.state}
        self.log.info("draft listing created", product_id=product.id, listing_id=draft.listing_id, title=title)
