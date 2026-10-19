"""In-house order handling shared by the Fulfiller agent and the print-queue page.

An Etsy order line becomes a FulfillmentOrder: the buyer's personalisation is parsed into the
design's fields, the print file is rendered at full resolution with those values (same template the
listing was built from), and a proof mockup on the ordered colour is made so the shop can check it.
"""

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath
from typing import Any

from pipeline.catalog import get_catalog
from pipeline.db.models import FulfillmentOrder, OrderStatus, Product
from pipeline.generators import merch, render

COLOR_KEYS = ("color", "colour", "primary color")
SIZE_KEYS = ("size",)


def pick_variation(variations: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for k, v in variations.items():
        if k.strip().lower() in keys:
            return v
    return None


def personalization_fields(product: Product) -> list[dict[str, Any]]:
    spec = ((product.brief_json or {}).get("creator_plan") or {}).get("merch_spec") or {}
    pers = spec.get("personalization") or {}
    return list(pers.get("fields") or []) if pers.get("enabled") else []


def simple_parse(raw: str, fields: list[dict[str, Any]]) -> dict[str, str]:
    """Deterministic fallback: one field takes the whole text; several split on newline, '/', '|' or ','."""
    raw = (raw or "").strip()
    if not fields or not raw:
        return {}
    if len(fields) == 1:
        return {fields[0]["key"]: raw}
    parts = [p.strip() for p in re.split(r"\n|/|\||,", raw) if p.strip()]
    return {f["key"]: parts[i] for i, f in enumerate(fields) if i < len(parts)}


def validate_values(values: dict[str, str], fields: list[dict[str, Any]]) -> list[str]:
    problems = []
    for f in fields:
        v = str(values.get(f["key"]) or "").strip()
        if not v:
            problems.append(f"missing {f.get('label') or f['key']}")
        elif len(v) > int(f.get("max_chars") or 40):
            problems.append(f"{f.get('label') or f['key']} is {len(v)} characters (max {f.get('max_chars')})")
    return problems


def render_order(order: FulfillmentOrder, product: Product, storage) -> list[str]:
    """(Re)render the order's print file and proof. Returns problems (empty when it is ready to print)."""
    plan = (product.brief_json or {}).get("creator_plan") or {}
    meta = plan.get("merch") or {}
    item = get_catalog().get(meta.get("item") or order.item)
    if item is None or not meta.get("template"):
        return ["the product has no print template"]
    html = storage.get(meta["template"]).decode("utf-8")
    art_uri = None
    if meta.get("art"):
        art_uri = "data:image/png;base64," + base64.b64encode(storage.get(meta["art"])).decode()
    values = {str(f["key"]): "" for f in personalization_fields(product)}  # a missing field prints blank, never "{{x}}"
    values.update({k: str(v) for k, v in (order.personalization or {}).items()})
    result = render.render_print_file(html, width_in=item.print_area["width_in"], height_in=item.print_area["height_in"],
                                      dpi=get_catalog().min_dpi, values=values, art_uri=art_uri)
    base = f"orders/{order.etsy_receipt_id}-{order.etsy_transaction_id}"
    order.print_file = storage.put(f"{base}/print-{PurePosixPath(base).name}.png", result.png, "image/png")
    color = order.color if order.color in item.colors else (meta.get("hero_color") or next(iter(item.colors)))
    order.proof_image = storage.put(f"{base}/proof.png", merch.to_png(merch.mockup(item, result.png, color, size=1200)),
                                    "image/png")
    return result.problems


def ledger(orders: list[FulfillmentOrder]) -> dict[str, Any]:
    done = [o for o in orders if o.status in (OrderStatus.PRINTING, OrderStatus.SHIPPED)]
    return {"owed_unpaid": round(sum(o.owed for o in done if not o.paid_to_shop), 2),
            "owed_total": round(sum(o.owed for o in done), 2), "items": sum(o.quantity for o in done)}
