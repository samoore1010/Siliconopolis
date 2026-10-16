"""Row-level claiming so concurrent/overlapping runs never double-process a product.

Uses compare-and-swap UPDATEs (works on Postgres and SQLite) instead of SELECT ... FOR UPDATE,
plus a stale-lock timeout so a crashed run does not strand rows forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from pipeline.db.models import Product, utcnow


def claim_products(
    session: Session,
    *,
    state: str,
    owner: str,
    limit: int,
    lock_timeout_minutes: int = 120,
    product_types: tuple[str, ...] | None = None,
) -> list[Product]:
    """Atomically claim up to `limit` products in `state` for `owner`. Returns the claimed rows."""
    if limit <= 0:
        return []
    stale_before = datetime.now(UTC) - timedelta(minutes=lock_timeout_minutes)
    q = (
        select(Product.id)
        .where(Product.state == state)
        .where(or_(Product.locked_by.is_(None), Product.locked_at < stale_before))
        .order_by(Product.created_at.asc(), Product.id.asc())
        .limit(limit * 2)
    )
    if product_types:
        q = q.where(Product.product_type.in_(product_types))
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
    rows.sort(key=lambda p: claimed.index(p.id))
    return list(rows)


def release(session: Session, product: Product) -> None:
    product.locked_by = None
    product.locked_at = None


def transition(session: Session, product: Product, new_state: str) -> None:
    """Move to a new state and release the lock in one place (keeps transitions auditable)."""
    product.state = new_state
    release(session, product)
