"""`python -m pipeline status`: counts by state, recent runs, spend. Also used by the web /status route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from pipeline.db import session_scope
from pipeline.db.models import Product, ProductState, Run
from pipeline.guardrails.caps import todays_spend


def collect_status(recent_runs: int = 10) -> dict[str, Any]:
    with session_scope() as s:
        counts = {state: 0 for state in ProductState.ALL}
        for state, n in s.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)).all():
            counts[state] = int(n)
        runs = s.execute(select(Run).order_by(Run.started_at.desc()).limit(recent_runs)).scalars().all()
        week_ago = datetime.now(UTC) - timedelta(days=7)
        spend_7d = float(s.execute(select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(Run.started_at >= week_ago)).scalar_one())
        spend_total = float(s.execute(select(func.coalesce(func.sum(Run.cost_usd), 0.0))).scalar_one())
        locked = int(s.execute(select(func.count(Product.id)).where(Product.locked_by.is_not(None))).scalar_one())
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "products_by_state": counts,
            "products_total": sum(counts.values()),
            "locked_rows": locked,
            "spend_usd": {"today": round(todays_spend(s), 4), "last_7_days": round(spend_7d, 4), "total": round(spend_total, 4)},
            "recent_runs": [
                {"id": r.id, "agent": r.agent, "status": r.status, "started_at": r.started_at.isoformat(),
                 "ended_at": r.ended_at.isoformat() if r.ended_at else None, "items": r.items_processed,
                 "cost_usd": round(r.cost_usd, 4), "errors": len(r.errors_json or [])}
                for r in runs
            ],
        }


def format_status(status: dict[str, Any]) -> str:
    lines = ["== Products by state =="]
    for state, n in status["products_by_state"].items():
        lines.append(f"  {state:<10} {n:>5}")
    lines.append(f"  {'total':<10} {status['products_total']:>5}   (locked rows: {status['locked_rows']})")
    sp = status["spend_usd"]
    lines.append("\n== Spend (USD) ==")
    lines.append(f"  today: {sp['today']:.4f}   last 7 days: {sp['last_7_days']:.4f}   total: {sp['total']:.4f}")
    lines.append("\n== Recent runs ==")
    if not status["recent_runs"]:
        lines.append("  (none)")
    for r in status["recent_runs"]:
        lines.append(f"  #{r['id']:<4} {r['agent']:<10} {r['status']:<12} items={r['items']:<3} cost={r['cost_usd']:.4f} "
                     f"errors={r['errors']} started={r['started_at'][:19]}")
    return "\n".join(lines)
