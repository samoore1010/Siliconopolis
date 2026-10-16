"""Volume and spend caps. Hitting a cap stops the run with a clear log line and a nonzero exit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.db.models import Product, ProductState, Run


class CapReached(RuntimeError):
    def __init__(self, cap: str, limit: float, current: float):
        self.cap, self.limit, self.current = cap, limit, current
        super().__init__(f"cap reached: {cap} (limit={limit}, current={current})")


def _utc_day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def todays_spend(session: Session, exclude_run_id: int | None = None) -> float:
    q = select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.started_at >= _utc_day_start())
    if exclude_run_id is not None:
        q = q.where(Run.id != exclude_run_id)
    return float(session.execute(q).scalar_one())


def check_spend_cap(session: Session, limit_usd: float, current_run_cost: float, exclude_run_id: int | None) -> None:
    total = todays_spend(session, exclude_run_id) + current_run_cost
    if total >= limit_usd:
        raise CapReached("MAX_DAILY_SPEND_USD", limit_usd, round(total, 4))


def drafts_today(session: Session) -> int:
    q = select(func.count(Product.id)).where(
        Product.state.in_([ProductState.DRAFTED, ProductState.PUBLISHED]),
        Product.updated_at >= _utc_day_start(),
    )
    return int(session.execute(q).scalar_one())


def briefs_this_week(session: Session) -> int:
    since = datetime.now(UTC) - timedelta(days=7)
    q = select(func.count(Product.id)).where(Product.created_at >= since)
    return int(session.execute(q).scalar_one())


def remaining_daily_drafts(session: Session, cap: int) -> int:
    return max(0, cap - drafts_today(session))


def remaining_weekly_briefs(session: Session, cap: int) -> int:
    return max(0, cap - briefs_this_week(session))
