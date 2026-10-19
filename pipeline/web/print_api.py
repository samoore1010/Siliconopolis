"""Print queue for the production partner (the family print shop): what to print, files, ship-to,
and buttons to mark jobs printing / shipped. Separate login (PRINT_USER) that sees only this.

Writes need the X-Siliconopolis header (a cross-site form cannot send it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from pipeline.catalog import get_catalog
from pipeline.db import session_scope
from pipeline.db.events import EventKind, record_event
from pipeline.db.models import FulfillmentOrder, OrderStatus, Product
from pipeline.fulfillment import ledger, personalization_fields, render_order, validate_values
from pipeline.storage import get_storage
from pipeline.web.auth import require_printer
from pipeline.web.village_state import _aware

router = APIRouter(prefix="/api/print")
CARRIERS = ("usps", "ups", "fedex", "dhl", "other")


def require_write(x_siliconopolis: str | None = Header(None)) -> None:
    if x_siliconopolis != "1":
        raise HTTPException(403, "missing X-Siliconopolis header")


class StatusIn(BaseModel):
    status: Literal["ready", "printing", "shipped", "cancelled"]
    carrier: str | None = Field(None, max_length=64)
    tracking_code: str | None = Field(None, max_length=128)
    note: str | None = Field(None, max_length=1000)


class PersonalizationIn(BaseModel):
    values: dict[str, str]


def _order(o: FulfillmentOrder, product: Product | None) -> dict[str, Any]:
    item = get_catalog().get(o.item)
    return {
        "id": o.id, "receipt_id": o.etsy_receipt_id, "status": o.status, "title": o.title,
        "item": item.label if item else o.item, "method": item.method if item else "", "size": o.size,
        "color": o.color, "color_hex": item.colors.get(o.color or "") if item else None, "quantity": o.quantity,
        "personalization_raw": o.personalization_raw, "personalization": o.personalization or {},
        "fields": personalization_fields(product) if product else [], "buyer_note": o.buyer_note,
        "ship_to": o.ship_to or {}, "review_reason": o.review_reason, "carrier": o.carrier,
        "tracking_code": o.tracking_code, "etsy_synced": o.etsy_synced, "notes": o.notes,
        "owed": o.owed, "paid_to_shop": o.paid_to_shop,
        "print_area": f"{item.print_area['name']} {item.print_area['width_in']}x{item.print_area['height_in']} in" if item else "",
        "created_at": _aware(o.created_at).isoformat() if o.created_at else None,
        "shipped_at": _aware(o.shipped_at).isoformat() if o.shipped_at else None,
        "print_url": f"/api/print/file/{o.id}/print" if o.print_file else None,
        "proof_url": f"/api/print/file/{o.id}/proof" if o.proof_image else None,
    }


@router.get("/orders")
def orders(view: Literal["open", "shipped", "all"] = Query("open"), role: str = Depends(require_printer)):
    with session_scope() as s:
        stmt = select(FulfillmentOrder).order_by(FulfillmentOrder.created_at.asc())
        if view == "open":
            stmt = stmt.where(FulfillmentOrder.status.in_(OrderStatus.OPEN))
        elif view == "shipped":
            stmt = stmt.where(FulfillmentOrder.status == OrderStatus.SHIPPED)
        rows = s.execute(stmt).scalars().all()
        products = {p.id: p for p in s.execute(select(Product).where(
            Product.id.in_({r.product_id for r in rows if r.product_id}))).scalars()} if rows else {}
        all_rows = s.execute(select(FulfillmentOrder)).scalars().all()
        counts = {st: sum(1 for r in all_rows if r.status == st) for st in OrderStatus.ALL}
        return {"role": role, "orders": [_order(o, products.get(o.product_id)) for o in rows], "counts": counts,
                "ledger": ledger(all_rows), "carriers": CARRIERS}


@router.get("/file/{order_id}/{kind}")
def order_file(order_id: int, kind: Literal["print", "proof"], role: str = Depends(require_printer)):
    with session_scope() as s:
        o = s.get(FulfillmentOrder, order_id)
        if not o:
            raise HTTPException(404, "unknown order")
        key = o.print_file if kind == "print" else o.proof_image
        name = "-".join(x for x in (f"order-{o.etsy_receipt_id}", o.item, o.size, o.color, f"x{o.quantity}") if x)
    if not key:
        raise HTTPException(404, "no file yet")
    data = get_storage().get(key)
    disposition = "attachment" if kind == "print" else "inline"
    filename = f"{name.replace(' ', '')}{'-proof' if kind == 'proof' else ''}.png"
    return Response(data, media_type="image/png", headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@router.post("/orders/{order_id}/status", dependencies=[Depends(require_write)])
def set_status(order_id: int, body: StatusIn, role: str = Depends(require_printer)):
    with session_scope() as s:
        o = s.get(FulfillmentOrder, order_id)
        if not o:
            raise HTTPException(404, "unknown order")
        if body.status == "shipped":
            if not (body.tracking_code or "").strip():
                raise HTTPException(422, "Enter the tracking number to mark it shipped.")
            o.carrier = (body.carrier or "usps").strip().lower()
            o.tracking_code = body.tracking_code.strip()
            o.shipped_at = datetime.now(UTC)
        if body.status == "ready" and o.status == OrderStatus.NEEDS_REVIEW and not o.print_file:
            raise HTTPException(409, "There is no print file yet; fix the personalisation first.")
        if body.note:
            o.notes = (o.notes + "\n" if o.notes else "") + f"[{role}] {body.note.strip()}"
        old, o.status = o.status, body.status
        if body.status == "ready":
            o.review_reason = None
        record_event(s, kind=EventKind.INFO, agent="fulfiller", product_id=o.product_id,
                     message=f"Order {o.etsy_receipt_id} ({o.item} {o.size or ''} {o.color or ''}): {old} -> {o.status} by {role}")
        s.flush()
        return {"order": _order(o, s.get(Product, o.product_id) if o.product_id else None)}


@router.post("/orders/{order_id}/personalization", dependencies=[Depends(require_write)])
def set_personalization(order_id: int, body: PersonalizationIn, role: str = Depends(require_printer)):
    """Fix what gets printed (e.g. after messaging the buyer) and re-render the print file."""
    with session_scope() as s:
        o = s.get(FulfillmentOrder, order_id)
        product = s.get(Product, o.product_id) if o and o.product_id else None
        if not o or not product:
            raise HTTPException(404, "unknown order")
        fields = personalization_fields(product)
        keys = {f["key"] for f in fields}
        o.personalization = {k: str(v).strip()[:200] for k, v in body.values.items() if k in keys}
        problems = validate_values(o.personalization, fields) + render_order(o, product, get_storage())
        o.status = OrderStatus.NEEDS_REVIEW if problems else OrderStatus.READY
        o.review_reason = "; ".join(problems) or None
        s.flush()
        return {"order": _order(o, product), "problems": problems}


@router.post("/ledger/paid", dependencies=[Depends(require_write)])
def mark_paid(role: str = Depends(require_printer)):
    """Owner only: record that everything printed so far has been paid to the shop."""
    if role != "owner":
        raise HTTPException(403, "only the shop owner can mark payouts")
    with session_scope() as s:
        rows = s.execute(select(FulfillmentOrder).where(
            FulfillmentOrder.status.in_((OrderStatus.PRINTING, OrderStatus.SHIPPED)),
            FulfillmentOrder.paid_to_shop.is_(False))).scalars().all()
        total = round(sum(r.owed for r in rows), 2)
        for r in rows:
            r.paid_to_shop = True
        record_event(s, kind=EventKind.INFO, agent="owner", message=f"Paid the print shop ${total:.2f} for {len(rows)} order line(s)")
        return {"paid": total, "lines": len(rows)}
