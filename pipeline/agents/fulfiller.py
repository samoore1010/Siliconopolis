"""Fulfiller: Etsy orders for in-house apparel/mugs -> print-ready jobs for the print shop.

Every run (hourly): pull paid, unshipped Etsy orders; for each new line on one of our in-house
products, read the buyer's personalisation, render the print file and a proof, and put it in the
shop's print queue (/print). Lines that need a human (unclear or inappropriate personalisation, a
name that will not fit) are flagged for review instead of being printed. When the shop marks a
receipt shipped with a tracking number, the tracking is sent back to Etsy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.etsy import get_etsy_client
from pipeline.catalog import get_catalog
from pipeline.db.events import EventKind
from pipeline.db.models import FulfillmentOrder, OrderStatus, Product, ProductType
from pipeline.fulfillment import (
    COLOR_KEYS,
    SIZE_KEYS,
    personalization_fields,
    pick_variation,
    render_order,
    simple_parse,
    validate_values,
)
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.storage import get_storage

from .base import Agent

LOOKBACK_DAYS = 45


class FulfillerAgent(Agent):
    name = "fulfiller"
    prompt_file = "order_check"

    def execute(self, session: Session) -> None:
        etsy = get_etsy_client()
        if not etsy.is_connected():
            self.log.info("Etsy not connected; no orders to fetch")
            return
        storage = get_storage()
        since = int((datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS)).timestamp())
        lines = etsy.get_open_orders(min_created=since)
        known = {r[0] for r in session.execute(select(FulfillmentOrder.etsy_transaction_id))}
        listing_ids = {ln.listing_id for ln in lines}
        products = {p.etsy_listing_id: p for p in session.execute(
            select(Product).where(Product.etsy_listing_id.in_(listing_ids),
                                  Product.product_type.in_(ProductType.PHYSICAL))).scalars()} if listing_ids else {}
        new = 0
        for line in lines:
            product = products.get(line.listing_id)
            if line.transaction_id in known or product is None:
                continue  # already queued, or a digital product (Etsy delivers those itself)
            try:
                self._queue(session, line, product, storage)
                new += 1
                self.items_processed += 1
            except CapReached:
                raise
            except Exception as e:  # noqa: BLE001
                self.note_error(f"fulfiller failed for order {line.receipt_id}/{line.transaction_id}: {e}",
                                product_id=product.id)
            session.commit()
        if new:
            self._event(EventKind.INFO, f"{new} new order line(s) added to the print queue")
        self._sync_shipments(session, etsy)

    def _queue(self, session: Session, line, product: Product, storage) -> None:
        plan = (product.brief_json or {}).get("creator_plan") or {}
        item = get_catalog().get((plan.get("merch") or {}).get("item"))
        order = FulfillmentOrder(
            etsy_receipt_id=line.receipt_id, etsy_transaction_id=line.transaction_id, product_id=product.id,
            item=item.key if item else "", title=line.title or product.brief_json.get("concept", ""),
            size=pick_variation(line.variations, SIZE_KEYS), color=pick_variation(line.variations, COLOR_KEYS),
            quantity=line.quantity, personalization_raw=line.personalization, buyer_note=line.buyer_note,
            ship_to=line.ship_to, unit_cost=float(item.unit_cost) if item else 0.0, status=OrderStatus.NEW)
        if item and not order.size and len(item.sizes) == 1:
            order.size = item.sizes[0]
        if item and not order.color and len(item.colors) == 1:
            order.color = next(iter(item.colors))
        session.add(order)
        session.flush()
        concerns: list[str] = []
        fields = personalization_fields(product)
        if fields:
            values, more = self._read_personalization(session, line, fields)
            order.personalization = values
            concerns += more + validate_values(values, fields)
            hits = get_blocklist().check_many(line.personalization, json.dumps(values))
            if hits:
                concerns.append(f"blocked words: {hits}")
        problems = render_order(order, product, storage)
        concerns += problems
        if concerns:
            order.status = OrderStatus.NEEDS_REVIEW
            order.review_reason = "; ".join(dict.fromkeys(concerns))[:1000]
        else:
            order.status = OrderStatus.READY
        self.log.info("order queued", order_id=order.id, status=order.status, product_id=product.id)

    def _read_personalization(self, session: Session, line, fields) -> tuple[dict, list[str]]:
        user = (f"Fields: {json.dumps(fields)}\nBuyer's personalisation text: {json.dumps(line.personalization)}\n"
                f"Buyer's note to seller: {json.dumps(line.buyer_note)}")
        try:
            result = self.ask(session, task="order_check", model=self.settings.model_cheap, user=user,
                              max_tokens=800, temperature=0.0)
            data = result.json()
            values = {f["key"]: str((data.get("values") or {}).get(f["key"]) or "").strip() for f in fields}
            return {k: v for k, v in values.items() if v}, [str(c) for c in data.get("concerns") or []]
        except CapReached:
            raise
        except Exception as e:  # noqa: BLE001 - fall back to a plain split, but have a person look
            return simple_parse(line.personalization, fields), [f"automatic check unavailable ({str(e)[:80]})"]

    def _sync_shipments(self, session: Session, etsy) -> None:
        rows = session.execute(select(FulfillmentOrder).where(FulfillmentOrder.status == OrderStatus.SHIPPED,
                                                              FulfillmentOrder.etsy_synced.is_(False))).scalars().all()
        for receipt_id in sorted({r.etsy_receipt_id for r in rows}):
            siblings = session.execute(select(FulfillmentOrder).where(
                FulfillmentOrder.etsy_receipt_id == receipt_id,
                FulfillmentOrder.status != OrderStatus.CANCELLED)).scalars().all()
            if any(s.status != OrderStatus.SHIPPED for s in siblings):
                continue  # wait until every item on the receipt has shipped
            first = next(s for s in siblings if s.tracking_code)
            if self.settings.dry_run:
                self.log.info("DRY_RUN: would send tracking to Etsy", receipt_id=receipt_id)
                continue
            try:
                etsy.create_receipt_shipment(receipt_id, carrier=first.carrier or "other", tracking_code=first.tracking_code)
            except Exception as e:  # noqa: BLE001
                self.note_error(f"could not send tracking for receipt {receipt_id} to Etsy: {e}")
                continue
            for s in siblings:
                s.etsy_synced = True
            self.items_processed += 1
            session.commit()
