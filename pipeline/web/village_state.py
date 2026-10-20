"""Pure derivation of village state from database rows (unit-tested without the web layer)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.agents import AGENT_NAMES
from pipeline.config import Settings
from pipeline.db.locking import _product_title
from pipeline.db.models import Event, Product, ProductState, Run, RunStatus
from pipeline.guardrails.caps import briefs_this_week, drafts_today, todays_spend


class AgentState:
    WORKING = "working"
    IDLE = "idle"
    SLEEPING = "sleeping"
    WAITING_HUMAN = "waiting_human"
    CAP_REACHED = "cap_reached"
    ERROR = "error"
    STALE_LOCK = "stale_lock"


# schedule interval per agent (for the "sleeping" rule: > 1.5x interval without a run)
SCHEDULE_HOURS = {"researcher": 24 * 7, "creator": 24, "editor": 1, "qa": 24, "lister": 24, "analyst": 24 * 7, "fulfiller": 1}
INPUT_STATE = {"researcher": None, "creator": ProductState.BRIEF, "editor": ProductState.EDITING, "qa": ProductState.CREATED,
               "lister": ProductState.APPROVED, "analyst": ProductState.DRAFTED, "fulfiller": None}
# which Railway cron service runs each agent (for the optional Run-now button)
SERVICE_FOR_AGENT = {"researcher": "researcher", "creator": "daily", "editor": "editor", "qa": "daily", "lister": "daily",
                     "analyst": "analyst", "fulfiller": "fulfiller"}
AGENT_META = {
    "researcher": {"title": "Researcher", "role": "finds what sells and proposes product ideas", "schedule": "weekly, Mon 6:00 AM Phoenix"},
    "creator": {"title": "Creator", "role": "designs the product files and listing photos", "schedule": "daily, 7:00 AM Phoenix"},
    "editor": {"title": "Editor", "role": "polishes products you send it, with a stronger model", "schedule": "hourly, only when you send it something"},
    "qa": {"title": "QA", "role": "checks quality, copyright and whether it would sell", "schedule": "daily, after Creator"},
    "lister": {"title": "Lister", "role": "writes the Etsy listing and saves it as a draft", "schedule": "daily, after QA"},
    "analyst": {"title": "Analyst", "role": "reads sales and tells the Researcher what works", "schedule": "weekly, Sun 8:00 PM Phoenix"},
    "fulfiller": {"title": "Fulfiller", "role": "turns shirt and mug orders into print jobs", "schedule": "hourly"},
}


@dataclass
class RunInfo:
    id: int
    status: str
    started_at: str
    ended_at: str | None
    items: int
    cost_usd: float
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, r: Run) -> RunInfo:
        return cls(r.id, r.status, r.started_at.isoformat(), r.ended_at.isoformat() if r.ended_at else None,
                   r.items_processed, round(r.cost_usd or 0.0, 4), list(r.errors_json or []))


def derive_agent_state(
    *,
    agent: str,
    open_run: bool,
    last_run_status: str | None,
    last_run_at: datetime | None,
    input_count: int,
    stale_locks: int,
    awaiting_human: int,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return (state, reason). Priority: working > error > cap > stale lock > waiting on human > sleeping > idle."""
    now = now or datetime.now(timezone.utc)
    if open_run:
        return AgentState.WORKING, "run in progress"
    if last_run_status == RunStatus.FAILED:
        return AgentState.ERROR, "last run failed"
    if last_run_status == RunStatus.STOPPED:
        return AgentState.IDLE, "stopped by you; will run again on schedule"
    if last_run_status == RunStatus.CAP_REACHED:
        return AgentState.CAP_REACHED, "last run stopped at a cap"
    if stale_locks:
        return AgentState.STALE_LOCK, f"{stale_locks} row(s) locked past the stale threshold"
    if agent == "lister" and awaiting_human:
        return AgentState.WAITING_HUMAN, f"{awaiting_human} draft(s) waiting for your review on Etsy"
    limit = timedelta(hours=SCHEDULE_HOURS.get(agent, 24) * 1.5)
    if agent == "editor" and input_count == 0:
        return AgentState.IDLE, "nothing to polish; send it a product from the Products page"
    if (last_run_at is None or now - last_run_at > limit) and input_count == 0:
        return AgentState.SLEEPING, "nothing queued and no recent run"
    return AgentState.IDLE, "waiting for the next scheduled run"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect_village_state(session: Session, settings: Settings) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stale_before = now - timedelta(minutes=settings.lock_timeout_minutes)

    queue = {s: 0 for s in ProductState.ALL}
    for state, n in session.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)):
        queue[state] = int(n)

    locked = session.execute(select(Product).where(Product.locked_by.is_not(None))).scalars().all()
    runs = session.execute(select(Run).order_by(Run.started_at.desc()).limit(300)).scalars().all()
    awaiting_human = queue[ProductState.DRAFTED]

    agents = []
    for name in AGENT_NAMES:
        agent_runs = [r for r in runs if r.agent == name]
        open_runs = [r for r in agent_runs if r.ended_at is None and r.status == RunStatus.RUNNING]
        finished = [r for r in agent_runs if r.ended_at is not None]
        last = finished[0] if finished else None
        my_locked = [p for p in locked if (p.locked_by or "").startswith(f"{name}:")]
        stale = [p for p in my_locked if p.locked_at and _aware(p.locked_at) < stale_before]
        input_state = INPUT_STATE[name]
        input_count = queue.get(input_state, 0) if input_state else 0
        state, reason = derive_agent_state(
            agent=name, open_run=bool(open_runs), last_run_status=last.status if last else None,
            last_run_at=_aware(last.started_at) if last else None, input_count=input_count,
            stale_locks=len(stale), awaiting_human=awaiting_human, now=now)
        today = [r for r in agent_runs if _aware(r.started_at) >= day_start]
        current_item = _product_title(my_locked[0]) if my_locked else None
        meta = AGENT_META[name]
        agents.append({
            "name": name, "title": meta["title"], "role": meta["role"], "schedule": meta["schedule"],
            "state": state, "state_reason": reason, "current_item": current_item,
            "input_state": input_state, "input_count": input_count, "locked_count": len(my_locked),
            "stale_locks": len(stale),
            "runs_today": len(today), "items_today": sum(r.items_processed for r in today),
            "cost_today": round(sum(r.cost_usd or 0.0 for r in today), 4),
            "last_run": asdict(RunInfo.from_row(last)) if last else None,
            "running": ({"run_id": open_runs[0].id, "started_at": _aware(open_runs[0].started_at).isoformat(),
                         "items": open_runs[0].items_processed, "stop_requested": bool(open_runs[0].stop_requested_at)}
                        if open_runs else None),
            "run_now_available": settings.run_now_enabled,
        })

    last_event_id = session.execute(select(func.max(Event.id))).scalar() or 0
    return {
        "generated_at": now.isoformat(),
        "env": {"app_env": settings.app_env, "mode": settings.mode, "dry_run": settings.dry_run,
                "llm_provider": settings.effective_llm_provider, "etsy_adapter": settings.adapter_mode("etsy"),
                "image_provider": settings.image_provider},
        "caps": {"daily_draft_cap": settings.daily_draft_cap, "drafts_today": drafts_today(session),
                 "max_daily_spend_usd": settings.max_daily_spend_usd, "spend_today": round(todays_spend(session), 4),
                 "weekly_brief_cap": settings.weekly_brief_cap, "briefs_this_week": briefs_this_week(session)},
        "queue": queue,
        "agents": agents,
        "outbox": {"drafted": queue[ProductState.DRAFTED], "published": queue[ProductState.PUBLISHED],
                   "awaiting_approval": awaiting_human},
        "reject_bin": {"count": queue[ProductState.REJECTED]},
        "stale_locks": sum(a["stale_locks"] for a in agents),
        "last_event_id": int(last_event_id),
        "run_now_enabled": settings.run_now_enabled,
    }


def serialize_event(e: Event) -> dict[str, Any]:
    return {"id": e.id, "ts": _aware(e.ts).isoformat(), "agent": e.agent, "product_id": e.product_id,
            "from_state": e.from_state, "to_state": e.to_state, "kind": e.kind, "message": e.message,
            "cost_usd": round(e.cost_usd or 0.0, 5)}


def product_summary(p: Product) -> dict[str, Any]:
    return {"id": p.id, "state": p.state, "niche": p.niche, "product_type": p.product_type,
            "title": _product_title(p), "etsy_listing_id": p.etsy_listing_id,
            "updated_at": _aware(p.updated_at).isoformat() if p.updated_at else None,
            "cost_usd": round(p.cost_usd or 0.0, 4), "n_images": len(p.image_paths or [])}
