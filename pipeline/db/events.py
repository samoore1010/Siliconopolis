"""Event writes. Events are append-only; `prune_events` enforces the retention window."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from pipeline.db.models import Event
from pipeline.logging_setup import current_agent

RETENTION_DAYS = 30


class EventKind:
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    TRANSITION = "transition"
    CAP_REACHED = "cap_reached"
    ERROR = "error"


def record_event(
    session: Session,
    *,
    kind: str,
    message: str = "",
    agent: str | None = None,
    product_id: int | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    cost_usd: float = 0.0,
) -> Event:
    ev = Event(
        agent=agent or current_agent() or "system",
        product_id=product_id,
        from_state=from_state,
        to_state=to_state,
        kind=kind,
        message=message[:2000],
        cost_usd=float(cost_usd or 0.0),
    )
    session.add(ev)
    session.flush()
    return ev


def prune_events(session: Session, days: int = RETENTION_DAYS) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return session.execute(delete(Event).where(Event.ts < cutoff)).rowcount or 0
