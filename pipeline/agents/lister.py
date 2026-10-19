"""Lister: approved -> Etsy DRAFT listing (state=drafted). Never publishes."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.catalog import get_catalog
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

PHYSICAL_DISCLOSURE = (
    "\n\n— How it's made —\nDesigned by us (with the help of AI tools for ideas and copy) and printed to order "
    "by our production partner, a small family-run print shop in Arizona. Original design; it does not "
    "reference any brand, team, character or artist."
)

# TODO(owner): confirm these against Etsy's current seller policy for AI-assisted, designed-by-seller
# digital items before go-live (who_made / when_made / is_supply / production partner disclosure).
PRODUCTION_ATTRIBUTES = {"who_made": "i_did", "when_made": "2020_2026", "is_supply": False}

TAXONOMY_HINTS = {
    ProductType.WALL_ART: ["digital prints", "prints", "wall decor"],
    ProductType.PLANNER: ["calendars & planners", "planners", "paper"],
    ProductType.TRACKER: ["calendars & planners", "planners", "paper"],
    ProductType.SPREADSHEET: ["spreadsheets", "digital templates", "templates"],
    ProductType.APPAREL: ["t-shirts", "sweatshirts", "hoodies", "clothing"],
    ProductType.MUG: ["mugs", "drinkware", "kitchen & dining"],
}
# Etsy's two free-form listing properties ("custom1"/"custom2"): used for Size and Colour variations.
SIZE_PROPERTY, COLOR_PROPERTY = 513, 514


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
        types = ProductType.DIGITAL
        if self.settings.etsy_shipping_profile_id:
            types = ProductType.DIGITAL + ProductType.PHYSICAL
        else:
            waiting = session.execute(select(func.count(Product.id)).where(
                Product.state == ProductState.APPROVED, Product.product_type.in_(ProductType.PHYSICAL))).scalar_one()
            if waiting:
                self._event(EventKind.INFO, f"{waiting} approved shirt/mug product(s) are waiting: set "
                            "ETSY_SHIPPING_PROFILE_ID (and ETSY_PRODUCTION_PARTNER_ID) so they can be drafted.")
        products = claim_products(session, state=ProductState.APPROVED, owner=owner, limit=remaining,
                                  lock_timeout_minutes=self.settings.lock_timeout_minutes,
                                  product_types=types)
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

    def _physical_payload(self, payload: dict, plan: dict, item, copy: dict) -> tuple[dict, dict]:
        """Made-to-order physical listing + Size x Colour inventory (with size upcharges)."""
        spec = plan.get("merch_spec") or {}
        pers = spec.get("personalization") or {}
        payload = {**payload, "type": "physical", "quantity": 99, "when_made": "made_to_order",
                   "shipping_profile_id": self.settings.etsy_shipping_profile_id,
                   "materials": [str(m)[:45] for m in (copy.get("materials") or [item.material, item.method])][:13]}
        for key, setting in (("readiness_state_id", "etsy_readiness_state_id"),
                             ("return_policy_id", "etsy_return_policy_id")):
            if getattr(self.settings, setting):
                payload[key] = getattr(self.settings, setting)
        if self.settings.etsy_production_partner_id:
            payload["production_partner_ids"] = [self.settings.etsy_production_partner_id]
        if pers.get("enabled"):
            fields = pers.get("fields") or []
            payload.update({"is_personalizable": True, "personalization_is_required": True,
                            "personalization_char_count_max": min(1024, max(20, sum(int(f.get("max_chars") or 20) + 3 for f in fields))),
                            "personalization_instructions": str(pers.get("instructions") or
                                                                "Enter: " + ", ".join(str(f.get("label")) for f in fields))[:256]})
        colors = [c for c in spec.get("colors") or [] if c in item.colors] or list(item.colors)[:1]
        products = []
        for size in item.sizes:
            for color in colors:
                products.append({
                    "sku": f"{item.key}-{size}-{color}".replace(" ", "").lower()[:32],
                    "property_values": [
                        {"property_id": SIZE_PROPERTY, "property_name": "Size", "values": [size]},
                        {"property_id": COLOR_PROPERTY, "property_name": "Color", "values": [color]}],
                    "offerings": [{"price": item.price_for(size), "quantity": 99, "is_enabled": True}]})
        inventory = {"products": products, "price_on_property": [SIZE_PROPERTY] if item.size_upcharge else [],
                     "quantity_on_property": [], "sku_on_property": [SIZE_PROPERTY, COLOR_PROPERTY]}
        return payload, inventory

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
        physical = product.product_type in ProductType.PHYSICAL
        item = get_catalog().get((plan.get("merch_spec") or {}).get("item")) if physical else None
        if physical and item is None:
            options = get_catalog().for_type(product.product_type)
            if not options:
                raise RuntimeError(f"catalog.yaml has no items for {product.product_type}")
            item = options[0]
        user = (f"Brief:\n{json.dumps(brief, indent=2)}\n\nProduction plan summary:\n"
                f"{json.dumps({k: plan.get(k) for k in ('title_hint', 'planner_spec', 'spreadsheet_spec', 'merch_spec')}, indent=2)}\n\n"
                + (f"PHYSICAL product, printed to order by our family print shop (production partner) and shipped. "
                   f"Product: {json.dumps(item.summary() if item else {})}; material: {item.material if item else ''}; "
                   f"ships in {'-'.join(str(d) for d in get_catalog().processing_days)} business days."
                   if physical else f"Files delivered: {[PurePosixPath(p).name for p in product.file_paths]}"))
        result = self.ask(session, task="lister_copy", model=self.settings.model_worker, user=user)
        product.add_cost(result.cost_usd)
        copy = result.json()

        title = str(copy.get("title", "")).strip()[:140]
        tags = [str(t).strip().lower()[:20] for t in copy.get("tags", []) if str(t).strip()]
        tags = list(dict.fromkeys(tags))[:13]
        if len(tags) < 13:
            fallback = [k.lower()[:20] for k in brief.get("keywords", [])] + (
                ["personalized gift", "custom gift", "gift for her", "gift for him"] if physical
                else ["digital download", "printable", "instant download"])
            for f in fallback:
                if len(tags) >= 13:
                    break
                if f not in tags:
                    tags.append(f)
        description = str(copy.get("description", "")).strip() + (PHYSICAL_DISCLOSURE if physical else AI_DISCLOSURE)
        if not title:
            raise RuntimeError("lister: empty title")
        hits = blocklist.check_many(title, " ".join(tags), description)
        if hits:
            raise RuntimeError(f"lister: blocklist hit in copy: {hits}")

        price = float(item.price if item else (brief.get("target_price_usd") or 5.99))
        taxonomy_id = self._pick_taxonomy(product.product_type, str(copy.get("taxonomy_hint", "")), taxonomy)
        payload = {
            "title": title, "description": description, "tags": tags, "price": round(price, 2), "quantity": 999,
            "taxonomy_id": taxonomy_id, "type": "download", "materials": copy.get("materials", ["digital file"])[:13],
            "state": "draft", **PRODUCTION_ATTRIBUTES,
        }
        inventory = None
        if physical:
            payload, inventory = self._physical_payload(payload, plan, item, copy)
        if self.settings.dry_run:
            self.log.info("DRY_RUN: would create draft listing", product_id=product.id, title=title)
            product.listing_json = {**payload, "dry_run": True, **({"inventory": inventory} if inventory else {})}
            return

        draft = etsy.create_draft_listing(payload)
        if inventory:
            etsy.update_listing_inventory(draft.listing_id, inventory)
        for rank, img in enumerate(product.image_paths[:10], start=1):
            etsy.upload_listing_image(draft.listing_id, storage.get(img), PurePosixPath(img).name, rank=rank)
        for f in ([] if physical else product.file_paths[:5]):  # Etsy allows up to 5 digital files per listing
            name = PurePosixPath(f).name
            etsy.upload_listing_file(draft.listing_id, storage.get(f), name, name=name[:70])

        product.etsy_listing_id = draft.listing_id
        product.listing_json = {**payload, "etsy_url": draft.url, "listing_state": draft.state,
                                **({"inventory": inventory} if inventory else {})}
        self.log.info("draft listing created", product_id=product.id, listing_id=draft.listing_id, title=title)
