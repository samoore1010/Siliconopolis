"""Row-level claiming so concurrent/overlapping runs never double-process a product.

Uses compare-and-swap UPDATEs (works on Postgres and SQLite) instead of SELECT ... FOR UPDATE,
plus a stale-lock timeout so a crashed run does not strand rows forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from pipeline.db.models import Product, utcnow

# Build/review order: owner-set priority first (lower = sooner), then oldest first.
QUEUE_ORDER = (func.coalesce(Product.priority, 2_000_000_000).asc(), Product.created_at.asc(), Product.id.asc())


def claim_products(
    session: Session,
    *,
    state: str,
    owner: str,
    limit: int,
    lock_timeout_minutes: int = 120,
    product_types: tuple[str, ...] | None = None,
    exclude_ids: set[int] | None = None,
) -> list[Product]:
    """Atomically claim up to `limit` products in `state` for `owner`. Returns the claimed rows."""
    if limit <= 0:
        return []
    stale_before = datetime.now(UTC) - timedelta(minutes=lock_timeout_minutes)
    q = (
        select(Product.id)
        .where(Product.state == state)
        .where(or_(Product.locked_by.is_(None), Product.locked_at < stale_before))
        .order_by(*QUEUE_ORDER)
        .limit(limit * 2)
    )
    if product_types:
        q = q.where(Product.product_type.in_(product_types))
    if exclude_ids:
        q = q.where(Product.id.not_in(exclude_ids))
    candidate_ids = [row[0] for row in session.execute(q)]

    claimed: list[int] = []
    for pid in candidate_ids:
        if len(claimed) >= limit:
            break
        result = session.execute(
            update(Product)
            .where(Product.id == pid)
            .where(Product.state == state)
            .where(or_(Product.locked_by.is_(None), Product.locked_at < stale_before))
            .values(locked_by=owner, locked_at=utcnow())
        )
        if result.rowcount == 1:
            claimed.append(pid)
    session.commit()
    if not claimed:
        return []
    rows = session.execute(select(Product).where(Product.id.in_(claimed))).scalars().all()
    rows.sort(key=lambda p: claimed.index(p.id))  # keep queue order
    return list(rows)


def release(session: Session, product: Product) -> None:
    product.locked_by = None
    product.locked_at = None


def transition(session: Session, product: Product, new_state: str, message: str = "") -> None:
    """Move to a new state, release the lock and log an event (keeps transitions auditable)."""
    from pipeline.db.events import EventKind, record_event

    old_state = product.state
    product.state = new_state
    release(session, product)
    title = _product_title(product)
    record_event(session, kind=EventKind.TRANSITION, product_id=product.id, from_state=old_state,
                 to_state=new_state, message=message or f"{title}: {old_state} -> {new_state}")


def _product_title(product: Product) -> str:
    brief = product.brief_json or {}
    plan = brief.get("creator_plan") or {}
    listing = product.listing_json or {}
    return str(listing.get("title") or plan.get("title_hint") or brief.get("concept") or f"product {product.id}")[:120]
